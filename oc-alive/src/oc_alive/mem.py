"""Memory regulator.

Oracle's idle test for A1 shapes includes *memory utilisation below 20%*, so we
keep a configurable slice of RAM resident as anonymous, page-touched memory.

Safety first: this regulator only ever fills the *gap* between what the machine
already uses and the target, and it gives memory straight back when

* ``MemAvailable`` drops below a floor,
* the kernel reports memory pressure (PSI), or
* a real workload claims more RAM than the target.

So if you start a build that needs 6 GiB, oc-alive releases its pages instead of
fighting for them.
"""

from __future__ import annotations

import logging
import mmap
import threading
import time

from . import metrics
from .priority import set_thread_name

log = logging.getLogger("oc-alive.mem")

MiB = 1024 * 1024


def _touch(block: mmap.mmap, size: int) -> None:
    """Fault in (and keep resident) every 4 KiB page of ``block``."""
    step = metrics.PAGE_SIZE
    for offset in range(0, size, step):
        block[offset] = 1


class MemRegulator:
    def __init__(self, cfg: dict) -> None:
        self._cfg = dict(cfg)
        self._stop = threading.Event()
        self._paused = False
        self._lock = threading.Lock()
        self._blocks: list[mmap.mmap] = []
        self._held = 0
        self.held_bytes = 0
        self.target_bytes = 0
        self.used_percent = 0.0
        self.available_bytes = 0
        self.last_action = "init"
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="oc-mem", daemon=True)
        self._thread.start()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._cfg.get("enabled", True))

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._release(self._held)

    def apply(self, cfg: dict) -> None:
        with self._lock:
            self._cfg = dict(cfg)

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    # -------------------------------------------------------------- internals

    def _grow(self, size: int) -> bool:
        try:
            # MAP_PRIVATE|MAP_ANONYMOUS: ordinary anonymous pages, so they show
            # up as *used* memory in the guest (AnonPages) - which is what the
            # hypervisor and any guest monitoring agent reports.  The mmap
            # module default (MAP_SHARED) would land them in shmem/page cache.
            block = mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE)
            _touch(block, size)
        except (OSError, MemoryError, ValueError) as exc:
            log.warning("cannot claim %d MiB: %s", size // MiB, exc)
            self.last_action = f"grow-failed:{exc}"
            return False
        with self._lock:
            self._blocks.append(block)
            self._held += size
        return True

    def _release(self, size: int) -> int:
        freed = 0
        with self._lock:
            while self._blocks and freed < size:
                block = self._blocks.pop()
                freed += len(block)
                self._held -= len(block)
                del block
        return freed

    def _retouch(self) -> None:
        with self._lock:
            blocks = list(self._blocks)
        for block in blocks:
            _touch(block, len(block))

    def _plan(self) -> int:
        """How many bytes we should be holding right now."""
        info = metrics.meminfo()
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        used = metrics.mem_used(info)
        self.available_bytes = available
        self.used_percent = (used / total * 100.0) if total else 0.0
        with self._lock:
            cfg = dict(self._cfg)
            held = self._held
        others = max(0, used - held)
        if not cfg.get("enabled", True) or self._paused:
            return 0
        target = int(total * float(cfg.get("target_percent", 45.0)) / 100.0)
        cap = int(total * float(cfg.get("max_percent", 55.0)) / 100.0)
        self.target_bytes = min(target, cap)
        desired = min(max(self.target_bytes - others, 0), cap)
        floor = int(float(cfg.get("min_available_mb", 1536)) * MiB)
        if available < floor:
            # give back whatever is needed to restore the floor
            desired = min(desired, max(0, held - (floor - available)))
            self.last_action = "released:low-available"
        some, full = metrics.psi_memory()
        if full > float(cfg.get("psi_full_limit", 0.5)):
            desired = min(desired, int(held * 0.5))
            self.last_action = f"released:psi-full({full:.2f})"
        elif some > float(cfg.get("psi_some_limit", 2.0)):
            desired = min(desired, int(held * 0.75))
            self.last_action = f"released:psi-some({some:.2f})"
        return max(0, desired)

    def _run(self) -> None:
        set_thread_name(threading.current_thread().name)
        with self._lock:
            interval = max(0.25, float(self._cfg.get("control_interval_ms", 2000)) / 1000.0)
        next_retouch = time.monotonic() + 60.0
        while not self._stop.is_set():
            with self._lock:
                interval = max(0.25, float(self._cfg.get("control_interval_ms", 2000)) / 1000.0)
                chunk = max(1, int(float(self._cfg.get("chunk_mb", 64)) * MiB))
                batch = max(1, int(self._cfg.get("grow_chunks_per_cycle", 6)))
                retouch_interval = max(30.0, float(self._cfg.get("retouch_interval_s", 300)))
            desired = self._plan()
            with self._lock:
                held = self._held
            if desired > held + chunk // 2:
                count = min(batch, max(1, (desired - held) // chunk))
                for _ in range(count):
                    if self._stop.is_set():
                        break
                    if not self._grow(chunk):
                        break
                if self._held != held:
                    self.last_action = f"grew to {self._held // MiB} MiB"
            elif desired < held - chunk // 2:
                freed = self._release(held - desired)
                if freed:
                    self.last_action = f"shrank by {freed // MiB} MiB"
            with self._lock:
                self.held_bytes = self._held
            now = time.monotonic()
            if now >= next_retouch:
                if self._held:
                    self._retouch()
                    self.last_action = "retouched"
                next_retouch = now + retouch_interval
            self._stop.wait(interval)

    # ---------------------------------------------------------------- status

    def snapshot(self) -> dict:
        info = metrics.meminfo()
        total = info.get("MemTotal", 0)
        with self._lock:
            held = self._held
            cfg = dict(self._cfg)
        used = metrics.mem_used(info)
        target = min(
            total * float(cfg.get("target_percent", 45.0)) / 100.0,
            total * float(cfg.get("max_percent", 55.0)) / 100.0,
        )
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "target_percent": float(cfg.get("target_percent", 45.0)),
            "held_mb": round(held / MiB, 1),
            "target_mb": round(target / MiB, 1),
            "used_percent": round((used / total * 100.0) if total else 0.0, 2),
            "available_mb": round(info.get("MemAvailable", 0) / MiB, 1),
            "chunks": len(self._blocks),
            "last_action": self.last_action,
        }
