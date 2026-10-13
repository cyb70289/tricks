"""The resident daemon: owns the regulators, the status file and systemd."""

from __future__ import annotations

import json
import logging
import os
import signal
import tempfile
import threading
import time
from pathlib import Path

from . import __version__, config as config_mod, metrics
from .cpu import CpuRegulator
from .mem import MemRegulator
from .net import NetRegulator
from .priority import notify, watchdog_ping

log = logging.getLogger("oc-alive")

PAUSE_FILE = "PAUSED"
STATE_FILE = "status.json"


class Daemon:
    def __init__(self, cfg: dict, config_path: Path | None = None) -> None:
        self.cfg = cfg
        self.config_path = Path(config_path) if config_path else config_mod.config_path()
        self.state = config_mod.state_dir()
        self._stop = threading.Event()
        self._reload = False
        self.started_at = time.time()
        self.cpu = CpuRegulator(cfg.get("cpu", {}))
        self.mem = MemRegulator(cfg.get("mem", {}))
        self.net = NetRegulator(cfg.get("net", {}))
        self._regs = {"cpu": self.cpu, "mem": self.mem, "net": self.net}
        self._running: set[str] = set()

    # ------------------------------------------------------------- lifecycle

    def _install_signals(self) -> None:
        def on_stop(signum, _frame):
            log.info("signal %s received, shutting down", signum)
            self._stop.set()

        def on_reload(_signum, _frame):
            self._reload = True

        signal.signal(signal.SIGTERM, on_stop)
        signal.signal(signal.SIGINT, on_stop)
        signal.signal(signal.SIGHUP, on_reload)

    def _pause_file(self) -> Path:
        return self.state / PAUSE_FILE

    def _paused(self) -> bool:
        if bool(self.cfg.get("paused")):
            return True
        return self._pause_file().exists()

    def _apply_pause(self, paused: bool) -> None:
        self.cpu.set_paused(paused)
        self.mem.set_paused(paused)
        self.net.set_paused(paused)

    def reload(self) -> None:
        try:
            self.cfg = config_mod.load(self.config_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("config reload failed, keeping old config: %s", exc)
            return
        self.cpu.apply(self.cfg.get("cpu", {}))
        self.mem.apply(self.cfg.get("mem", {}))
        self.net.apply(self.cfg.get("net", {}))
        self._sync_regulators()
        log.info("configuration reloaded from %s", self.config_path)

    def _sync_regulators(self) -> None:
        """Start/stop regulator threads as their 'enabled' flags change."""
        for name in ("net", "mem", "cpu"):
            regulator = self._regs[name]
            wanted = regulator.enabled
            if wanted and name not in self._running:
                regulator.start()
                self._running.add(name)
                log.info("%s regulator started", name)
            elif not wanted and name in self._running:
                regulator.stop()
                self._running.discard(name)
                log.info("%s regulator stopped (disabled in config)", name)

    def run(self) -> int:
        logging.basicConfig(
            level=os.environ.get("OC_ALIVE_LOGLEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            stream=None,
        )
        self.state.mkdir(parents=True, exist_ok=True)
        self._install_signals()
        log.info(
            "oc-alive %s starting: cpus=%d mem=%.1f GiB iface=%s config=%s",
            __version__,
            metrics.online_cpus(),
            metrics.meminfo().get("MemTotal", 0) / (1024 ** 3),
            metrics.default_interface() or "?",
            self.config_path,
        )
        paused = self._paused()
        self._apply_pause(paused)
        self._sync_regulators()
        notify("READY=1")
        logging.getLogger("oc-alive.cpu").info(
            "burners use scheduler %s", self.cpu.priority
        )

        status_interval = max(1.0, float(self.cfg.get("status_interval_s", 5)))
        log_interval = max(5.0, float(self.cfg.get("log_interval_s", 60)))
        next_status = 0.0
        next_log = time.monotonic() + log_interval
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if self._reload:
                    self._reload = False
                    self.reload()
                paused_now = self._paused()
                if paused_now != paused:
                    paused = paused_now
                    self._apply_pause(paused)
                    log.info("paused=%s", paused)
                if now >= next_status:
                    self.write_status()
                    watchdog_ping()
                    notify("STATUS=keeping the instance busy")
                    next_status = now + status_interval
                if now >= next_log:
                    self.log_summary()
                    next_log = now + log_interval
                self._stop.wait(0.5)
        finally:
            notify("STOPPING=1")
            for name in ("net", "mem", "cpu"):
                if name in self._running:
                    self._regs[name].stop()
            self._running.clear()
            self.write_status()
            log.info("oc-alive stopped")
        return 0

    # ---------------------------------------------------------------- status

    def build_status(self) -> dict:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.started_at)),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "uptime_s": round(time.time() - self.started_at, 1),
            "paused": self._paused(),
            "config_path": str(self.config_path),
            "oracle_idle_threshold_percent": config_mod.IDLE_THRESHOLD_PERCENT,
            "cpu": self.cpu.snapshot(),
            "mem": self.mem.snapshot(),
            "net": self.net.snapshot(),
            "mirrors": self.net._pool.snapshot(),  # noqa: SLF001 - status only
            "default_interval_guard": (
                "Oracle marks an instance idle only when CPU AND network AND memory "
                "are all below 20% over 7 days, so beating the bar on CPU alone is "
                "enough - which is why memory and network are off by default."
            ),
        }

    def write_status(self) -> None:
        path = self.state / STATE_FILE
        try:
            payload = json.dumps(self.build_status(), indent=2)
            fd, tmp = tempfile.mkstemp(dir=str(self.state), prefix=".status.")
            with os.fdopen(fd, "w") as handle:
                handle.write(payload + "\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("could not write %s: %s", path, exc)

    def log_summary(self) -> None:
        cpu = self.cpu.snapshot()
        mem = self.mem.snapshot()
        net = self.net.snapshot()
        log.info(
            "cpu host=%.1f%% target=%.1f%% duty=%.2f | mem held=%sMiB used=%.1f%% "
            "avail=%sMiB | net %s rx=%.0f tx=%.0f rate=%.0f target=%.0f Mbps (%s)",
            cpu["host_percent"], cpu["target_percent"], cpu["duty"],
            mem["held_mb"], mem["used_percent"], mem["available_mb"],
            net["mode"], net["rx_mbps"], net["tx_mbps"],
            net["rate_mbps"], net["target_mbps"], net["last_action"],
        )
