"""Network regulator.

OCI also looks at network utilisation, so by default we pull a configurable
fraction of the instance's link budget through the VNIC and throw it away.
Two design choices keep this friendly:

* **deficit only** - an integral controller reads the real rx+tx counters and
  only adds whatever is missing to reach the target, so traffic you generate
  yourself (ssh, a proxy, apt, ...) counts towards the target;
* **free direction** - we only *download*.  OCI bills egress, not ingress, and
  TCP ACKs contribute a small amount of tx on their own.

If every mirror is unreachable (or ``mode = loopback``) the workers fall back to
a local 127.0.0.1 sink, which keeps the box busy without touching the WAN.
"""

from __future__ import annotations

import http.client
import logging
import socket
import threading
import time
from urllib.parse import urlsplit

from . import metrics
from .priority import apply_priority

log = logging.getLogger("oc-alive.net")

MiB = 1024 * 1024
GiB = 1024 * MiB
BYTES_PER_MBIT = 1_000_000 / 8
UA = "oc-alive/1.0 (+idle-guard)"


def _compute_target_mbps(cfg: dict) -> float:
    """Target VNIC throughput in Mbit/s (0 when there is nothing to ask for)."""
    link = float(cfg.get("link_mbps", 0) or 0) or metrics.online_cpus() * 1000.0
    target = link * float(cfg.get("target_percent", 45.0)) / 100.0
    cap = cfg.get("max_mbps", 0)
    if cap:
        target = min(target, float(cap))
    return max(0.0, target)


class RateLimiter:
    """Token bucket shared by every worker: the aggregate rate is what matters."""

    def __init__(self, burst_s: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._rate = 0.0
        self._allowance = 0.0
        self._last = time.monotonic()
        self._burst_s = burst_s

    @property
    def rate(self) -> float:
        with self._lock:
            return self._rate

    def set_rate(self, bytes_per_s: float) -> None:
        bytes_per_s = max(0.0, float(bytes_per_s))
        with self._lock:
            if self._rate <= 0.0 < bytes_per_s:
                self._last = time.monotonic()
                self._allowance = 0.0
            self._rate = bytes_per_s

    def pay(self, size: int, stop: threading.Event) -> bool:
        """Block until ``size`` bytes are allowed; False if we were stopped."""
        while not stop.is_set():
            with self._lock:
                now = time.monotonic()
                rate = self._rate
                if rate <= 0.0:
                    wait = 0.25
                else:
                    self._allowance = min(
                        self._allowance + (now - self._last) * rate,
                        rate * self._burst_s,
                    )
                    self._last = now
                    if self._allowance >= size:
                        self._allowance -= size
                        return True
                    wait = min((size - self._allowance) / rate, 0.5)
            stop.wait(max(wait, 0.002))
        return False


class MirrorPool:
    """Picks mirrors by observed throughput, with per-mirror backoff.

    Every mirror starts with a neutral score and the score is replaced by an
    EMA of the measured download rate (MiB/s).  A mirror that is currently
    being used by another worker is penalised, so workers spread out instead of
    all hammering the same URL, and slow or broken mirrors are simply not
    chosen while a fast one is available.
    """

    def __init__(self, mirrors: list[str], prior_mib_s: float = 1.0) -> None:
        self.urls = [str(url) for url in mirrors if str(url).strip()]
        self._state = {
            url: {"fails": 0, "until": 0.0, "score": prior_mib_s, "inflight": 0}
            for url in self.urls
        }
        self._lock = threading.Lock()

    def next(self) -> str | None:
        with self._lock:
            now = time.monotonic()
            healthy = [u for u in self.urls if self._state[u]["until"] <= now]
            if not healthy:
                return None
            chosen = max(
                healthy,
                key=lambda u: self._state[u]["score"] / (1 + self._state[u]["inflight"]),
            )
            self._state[chosen]["inflight"] += 1
            return chosen

    def done(self, url: str, size: int, seconds: float) -> None:
        """Release the in-flight slot and update the throughput score."""
        with self._lock:
            state = self._state.get(url)
            if state is None:
                return
            state["inflight"] = max(0, state["inflight"] - 1)
            if size > 4 * MiB and seconds > 0.2:
                observed = size / seconds / MiB
                state["score"] = 0.5 * state["score"] + 0.5 * observed

    def ok(self, url: str) -> None:
        with self._lock:
            if url in self._state:
                self._state[url]["fails"] = 0
                self._state[url]["until"] = 0.0

    def failed(self, url: str) -> None:
        with self._lock:
            state = self._state.setdefault(
                url, {"fails": 0, "until": 0.0, "score": 1.0, "inflight": 0}
            )
            state["fails"] += 1
            state["score"] *= 0.5
            state["until"] = time.monotonic() + min(30.0 * 2 ** (state["fails"] - 1), 900.0)

    def all_down(self) -> bool:
        with self._lock:
            now = time.monotonic()
            return bool(self._state) and all(
                state["until"] > now for state in self._state.values()
            )

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {
                url: {
                    "fails": state["fails"],
                    "mib_s": round(state["score"], 2),
                    "inflight": state["inflight"],
                    "cooling_down_s": round(max(0.0, state["until"] - now), 1),
                }
                for url, state in self._state.items()
            }


class _DiscardSink(threading.Thread):
    """Local TCP server that reads and throws away whatever is sent to it."""

    def __init__(self) -> None:
        super().__init__(name="oc-alive-sink", daemon=True)
        self._stop = threading.Event()
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(16)
        self._socket.settimeout(0.5)
        self.port = self._socket.getsockname()[1]

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except (OSError, socket.timeout):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(1.0)
        try:
            while not self._stop.is_set():
                try:
                    if not conn.recv(262144):
                        break
                except socket.timeout:
                    continue
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass


class NetRegulator:
    def __init__(self, cfg: dict) -> None:
        self._cfg = dict(cfg)
        self._stop = threading.Event()
        self._paused = False
        self._lock = threading.Lock()
        self._limiter = RateLimiter()
        self._pool = MirrorPool(cfg.get("mirrors") or [])
        self._meter = metrics.NetMeter(str(cfg.get("interface") or ""))
        self._threads: list[threading.Thread] = []
        self._sink: _DiscardSink | None = None
        self._rate_bps = 0.0
        self._downloaded = 0
        self._budget_day = time.strftime("%Y-%m-%d", time.gmtime())
        self.rx_mbps = 0.0
        self.tx_mbps = 0.0
        self.rate_mbps = 0.0
        self.loopback_bytes = 0
        self.target_mbps = 0.0
        self.host_percent = 0.0
        self.last_action = "init"

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._stop.clear()
        self._threads = []
        if self._sink is None and (
            self._cfg.get("mode") == "loopback" or self._cfg.get("fallback_loopback", True)
        ):
            self._sink = _DiscardSink()
            self._sink.start()
        self._spawn("oc-alive-net-ctl", self._control)
        workers = max(1, int(self._cfg.get("workers", 2)))
        for index in range(workers):
            self._spawn(f"oc-alive-net-{index}", self._worker)

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._cfg.get("enabled", True))

    def _spawn(self, name: str, target) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._limiter.set_rate(0.0)
        for thread in self._threads:
            thread.join(timeout=timeout)
        if self._sink is not None:
            self._sink.stop()
            self._sink = None
        # clear live readings so a stopped regulator cannot report stale rates
        self.rx_mbps = self.tx_mbps = self.rate_mbps = 0.0
        self.last_action = "disabled"

    def apply(self, cfg: dict) -> None:
        with self._lock:
            self._cfg = dict(cfg)
            self._pool = MirrorPool(cfg.get("mirrors") or [])
            iface = str(cfg.get("interface") or "")
            if iface != self._meter.interface:
                self._meter = metrics.NetMeter(iface)

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._limiter.set_rate(0.0)

    # -------------------------------------------------------------- internals

    def _target_bps(self) -> float:
        target_bps = _compute_target_mbps(self._cfg) * BYTES_PER_MBIT
        self.target_mbps = target_bps / BYTES_PER_MBIT
        return target_bps

    def _budget_exhausted(self) -> bool:
        with self._lock:
            budget_gb = float(self._cfg.get("daily_budget_gb", 0.0) or 0.0)
        if budget_gb <= 0:
            return False
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self._budget_day:
            self._budget_day = day
            self._downloaded = 0
        return self._downloaded >= budget_gb * GiB

    def _add_downloaded(self, size: int) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self._budget_day:
            self._budget_day = day
            self._downloaded = 0
        self._downloaded += max(0, size)

    def _control(self) -> None:
        apply_priority("other", 0)
        cpu_meter = metrics.CpuMeter()
        self._meter.sample()
        self._limiter.set_rate(0.0)
        while not self._stop.is_set():
            with self._lock:
                cfg = dict(self._cfg)
                interval = max(0.25, float(cfg.get("control_interval_ms", 2000)) / 1000.0)
                gain = float(cfg.get("gain", 0.7))
            self._stop.wait(interval)
            if self._stop.is_set():
                break
            rx, tx = self._meter.sample()
            self.rx_mbps = rx * 8 / 1_000_000
            self.tx_mbps = tx * 8 / 1_000_000
            self.host_percent = cpu_meter.sample()
            target_bps = self._target_bps()
            enabled = bool(cfg.get("enabled", True)) and not self._paused
            if not enabled:
                self._rate_bps = 0.0
                self.last_action = "disabled"
            elif str(cfg.get("mode", "external")) == "loopback":
                self._rate_bps = target_bps
                self.last_action = "loopback"
            elif self._budget_exhausted():
                self._rate_bps = 0.0
                self.last_action = "daily-budget-reached"
            elif self.host_percent > float(cfg.get("cpu_guard_percent", 80.0)):
                self._rate_bps = 0.0
                self.last_action = "cpu-guard"
            else:
                measured = rx + tx
                error = target_bps - measured
                if abs(error) > target_bps * 0.05 or self._rate_bps > 0:
                    self._rate_bps = max(
                        0.0,
                        min(self._rate_bps + error * gain, self._target_bps() * 1.25),
                    )
                self.last_action = "external"
            self._limiter.set_rate(self._rate_bps)
            self.rate_mbps = self._rate_bps / BYTES_PER_MBIT

    def _worker(self) -> None:
        with self._lock:
            policy = str(self._cfg.get("scheduler", "batch"))
            nice = int(self._cfg.get("nice", 19))
        apply_priority(policy, nice)
        while not self._stop.is_set():
            with self._lock:
                cfg = dict(self._cfg)
                chunk = max(4096, int(float(cfg.get("chunk_kib", 256)) * 1024))
            if self._paused or not cfg.get("enabled", True):
                self._stop.wait(0.5)
                continue
            if str(cfg.get("mode", "external")) == "loopback":
                self._loopback(5.0, chunk)
                continue
            url = self._pool.next()
            if url is None:
                self.last_action = "all-mirrors-cooling-down"
                if cfg.get("fallback_loopback", True):
                    self._loopback(10.0, chunk)
                else:
                    self._stop.wait(2.0)
                continue
            started = time.monotonic()
            total = 0
            try:
                total = self._download(url, chunk, cfg)
                self._pool.ok(url)
            except Exception as exc:  # noqa: BLE001 - any network error rotates
                log.debug("mirror %s failed: %s", url, exc)
                self._pool.failed(url)
                self.last_action = f"mirror-failed:{urlsplit(url).hostname}"
                if self._pool.all_down() and cfg.get("fallback_loopback", True):
                    self._loopback(10.0, chunk)
                else:
                    self._stop.wait(0.5)
            finally:
                self._pool.done(url, total, time.monotonic() - started)

    def _download(self, url: str, chunk: int, cfg: dict) -> int:
        parts = urlsplit(url)
        if not parts.hostname:
            raise ValueError(f"bad mirror url: {url!r}")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        timeout = float(cfg.get("timeout_s", 20))
        conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
        try:
            conn.request(
                "GET",
                path,
                headers={"User-Agent": UA, "Accept": "*/*", "Connection": "close"},
            )
            response = conn.getresponse()
            if response.status not in (200, 206):
                raise RuntimeError(f"HTTP {response.status}")
            total = 0
            read = response.read
            while not self._stop.is_set():
                data = read(chunk)
                if not data:
                    break
                total += len(data)
                self._add_downloaded(len(data))
                if not self._limiter.pay(len(data), self._stop):
                    break
            return total
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _loopback(self, seconds: float, chunk: int) -> None:
        """Local traffic: keeps the CPU (and memory bus) busy, no WAN usage."""
        sink = self._sink
        if sink is None:
            self._stop.wait(seconds)
            return
        payload = b"\0" * chunk
        deadline = time.monotonic() + seconds
        try:
            sock = socket.create_connection(("127.0.0.1", sink.port), timeout=5)
        except OSError:
            self._stop.wait(0.5)
            return
        try:
            while not self._stop.is_set() and time.monotonic() < deadline:
                if not self._limiter.pay(len(payload), self._stop):
                    break
                sock.sendall(payload)
                self.loopback_bytes += len(payload)
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # ---------------------------------------------------------------- status

    def snapshot(self) -> dict:
        with self._lock:
            cfg = dict(self._cfg)
        enabled_now = bool(cfg.get("enabled", True))
        link = float(cfg.get("link_mbps", 0) or 0) or metrics.online_cpus() * 1000.0
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "mode": str(cfg.get("mode", "external")),
            "interface": self._meter.interface,
            "link_mbps": round(link, 1),
            "target_percent": float(cfg.get("target_percent", 40.0)),
            "target_mbps": round(_compute_target_mbps(cfg), 1),
            "rate_mbps": round(self.rate_mbps if enabled_now else 0.0, 1),
            "rx_mbps": round(self.rx_mbps if enabled_now else 0.0, 2),
            "tx_mbps": round(self.tx_mbps if enabled_now else 0.0, 2),
            "downloaded_gib_today": round(self._downloaded / GiB, 3),
            "loopback_mib": round(self.loopback_bytes / MiB, 1),
            "budget_gib": float(cfg.get("daily_budget_gb", 0.0) or 0.0),
            "host_percent_seen": round(self.host_percent, 2),
            "last_action": self.last_action if enabled_now else "disabled",
        }
