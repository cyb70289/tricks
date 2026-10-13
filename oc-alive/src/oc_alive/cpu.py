"""CPU regulator.

Keeps *whole machine* CPU utilisation at ``target_percent`` by duty-cycling a
few burner threads.  Two properties matter:

* the feedback loop measures the real host utilisation, so any work you do
  yourself already counts towards the target and the burners back off;
* the burners run in SCHED_BATCH/SCHED_IDLE at nice 19, so as soon as a normal
  task wants a core, the burners are squeezed out.

The burn itself is ``hashlib`` over a 1 MiB buffer: OpenSSL releases the GIL,
so N threads really do use N cores (a pure-Python spin loop would cap out at
one core because of the GIL).
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time

from . import metrics
from .priority import apply_priority, priority_label

log = logging.getLogger("oc-alive.cpu")

_BUFFER = os.urandom(1 << 20)


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


class CpuRegulator:
    def __init__(self, cfg: dict) -> None:
        self._cfg = dict(cfg)
        self._stop = threading.Event()
        self._paused = False
        self._lock = threading.Lock()
        self.host_percent = 0.0
        self.duty = 0.0  # what we actually ask the burners for
        self.yields = 0  # slices abandoned early because a task was waiting
        self.target_percent = float(cfg.get("target_percent", 45.0))
        self.threads = int(cfg.get("threads") or 0) or metrics.online_cpus()
        self._threads: list[threading.Thread] = []
        self.priority = priority_label()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._stop.clear()
        self._threads = []
        self._spawn("oc-alive-cpu-ctl", self._control)
        for index in range(self.threads):
            self._spawn(f"oc-alive-cpu-{index}", self._burn)

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._cfg.get("enabled", True))

    def _spawn(self, name: str, target) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)

    def apply(self, cfg: dict) -> None:
        with self._lock:
            previous = self.threads
            self._cfg = dict(cfg)
            self.target_percent = float(cfg.get("target_percent", self.target_percent))
            self.threads = int(cfg.get("threads") or 0) or metrics.online_cpus()
        if self.threads != previous:
            log.warning(
                "cpu.threads changed %d -> %d; restart the unit to resize the burner pool",
                previous, self.threads,
            )

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            with self._lock:
                self.duty = 0.0

    # -------------------------------------------------------------- internals

    def _desired_target(self) -> float:
        with self._lock:
            if not self._cfg.get("enabled", True):
                return 0.0
            target = min(self.target_percent, float(self._cfg.get("max_percent", 100.0)))
        return _clamp(target, 0.0, 100.0) / 100.0

    def _control(self) -> None:
        apply_priority("other", 0)  # the controller itself must stay responsive
        meter = metrics.CpuMeter()
        while not self._stop.is_set():
            with self._lock:
                interval = max(0.25, float(self._cfg.get("control_interval_ms", 2000)) / 1000.0)
                gain_up = float(self._cfg.get("gain_up", 0.55))
                gain_down = float(self._cfg.get("gain_down", 0.35))
            self._stop.wait(interval)
            if self._stop.is_set():
                break
            self.host_percent = meter.sample()
            if self._paused:
                with self._lock:
                    self.duty = 0.0
                continue
            target = self._desired_target()
            error = target - self.host_percent / 100.0
            gain = gain_up if error > 0 else gain_down
            with self._lock:
                self.duty = _clamp(self.duty + error * gain, 0.0, 1.0)

    def _burn(self) -> None:
        with self._lock:
            policy = str(self._cfg.get("scheduler", "batch"))
            nice = int(self._cfg.get("nice", 19))
        apply_priority(policy, nice)
        self.priority = priority_label()
        buffer = _BUFFER
        nproc = metrics.online_cpus()
        while not self._stop.is_set():
            with self._lock:
                duty = self.duty
                cycle = max(0.02, float(self._cfg.get("cycle_ms", 500)) / 1000.0)
                jitter = max(0.0, float(self._cfg.get("jitter_percent", 15.0))) / 100.0
                yield_contended = bool(self._cfg.get("yield_when_contended", True))
                check_s = max(0.01, float(self._cfg.get("contended_check_ms", 50)) / 1000.0)
            if jitter:
                duty *= 1.0 + random.uniform(-jitter, jitter)
            duty = _clamp(duty, 0.0, 1.0)
            busy = cycle * duty
            started = time.monotonic()
            if busy > 0.002 and yield_contended and metrics.procs_running() > nproc:
                # Another task is already waiting for a CPU: burn nothing at all
                # this slice rather than competing with it.
                self.yields += 1
                busy = 0.0
            if busy > 0.002:
                deadline = started + busy
                next_check = started + check_s
                hasher = hashlib.sha256()
                while True:
                    hasher.update(buffer)
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    if yield_contended and now >= next_check:
                        next_check = now + check_s
                        if metrics.procs_running() > nproc:
                            # Someone else is waiting for a CPU: give it up now
                            # instead of finishing the slice.
                            self.yields += 1
                            break
                hasher.digest()
            rest = cycle - (time.monotonic() - started)
            if rest > 0:
                self._stop.wait(rest)

    # ---------------------------------------------------------------- status

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": bool(self._cfg.get("enabled", True)),
                "target_percent": self.target_percent,
                "max_percent": float(self._cfg.get("max_percent", 100.0)),
                "host_percent": round(self.host_percent, 2),
                "duty": round(self.duty, 4),
                "threads": self.threads,
                "scheduler": str(self._cfg.get("scheduler", "batch")),
                "nice": int(self._cfg.get("nice", 19)),
                "yield_when_contended": bool(self._cfg.get("yield_when_contended", True)),
                "yields": self.yields,
            }
