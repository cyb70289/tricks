"""Host metrics read straight out of procfs / sysfs (no dependencies)."""

from __future__ import annotations

import os
from pathlib import Path

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") or 4096


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# --------------------------------------------------------------------------- CPU


def cpu_snapshot() -> tuple[float, float]:
    """Return ``(busy, total)`` jiffies for the whole machine.

    ``iowait`` is counted as idle: the CPU was not doing work, and for
    reclamation purposes what matters is that the guest looks busy.
    """
    line = _read_text("/proc/stat").split("\n", 1)[0].split()
    values = [float(part) for part in line[1:9]]
    values += [0.0] * (8 - len(values))
    user, nice, system, idle, iowait, irq, softirq, steal = values
    total = user + nice + system + idle + iowait + irq + softirq + steal
    return total - idle - iowait, total


class CpuMeter:
    """Tracks whole-machine utilisation between samples."""

    def __init__(self) -> None:
        self._prev = cpu_snapshot()
        self.percent = 0.0

    def sample(self) -> float:
        cur = cpu_snapshot()
        d_total = cur[1] - self._prev[1]
        d_busy = cur[0] - self._prev[0]
        self._prev = cur
        self.percent = (d_busy / d_total * 100.0) if d_total > 0 else 0.0
        return self.percent


# ---------------------------------------------------------------------- memory


def meminfo() -> dict[str, int]:
    """Return /proc/meminfo values in bytes."""
    out: dict[str, int] = {}
    for line in _read_text("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                out[key] = int(parts[0]) * 1024
            except ValueError:
                continue
    return out


def mem_used(info: dict[str, int]) -> int:
    """What ``free`` calls ``used``: RAM neither free nor reclaimable cache."""
    cache = (
        info.get("Buffers", 0)
        + info.get("Cached", 0)
        + info.get("SReclaimable", 0)
    )
    return max(0, info.get("MemTotal", 0) - info.get("MemFree", 0) - cache)


def psi_memory() -> tuple[float, float]:
    """Return ``(some avg10, full avg10)`` memory stall percentages."""
    try:
        text = _read_text("/proc/pressure/memory")
    except OSError:
        return 0.0, 0.0
    some = full = 0.0
    for line in text.splitlines():
        kind, _, rest = line.partition(" ")
        for token in rest.split():
            if token.startswith("avg10="):
                try:
                    value = float(token.split("=", 1)[1])
                except ValueError:
                    continue
                if kind == "some":
                    some = value
                elif kind == "full":
                    full = value
    return some, full


# --------------------------------------------------------------------- network


def default_interface() -> str:
    """Interface carrying the default route ('' if none)."""
    try:
        for line in _read_text("/proc/net/route").splitlines()[1:]:
            fields = line.split()
            if len(fields) > 3 and fields[1] == "00000000" and fields[3] == "0003":
                return fields[0]
    except OSError:
        pass
    return ""


class NetMeter:
    """Measures VNIC throughput in bits per second."""

    def __init__(self, interface: str = "") -> None:
        self.interface = interface or default_interface()
        self._prev: tuple[int, int] | None = None
        self._prev_ts = 0.0

    def _counters(self) -> tuple[int, int] | None:
        base = Path("/sys/class/net") / self.interface / "statistics"
        try:
            rx = int((base / "rx_bytes").read_text())
            tx = int((base / "tx_bytes").read_text())
        except (OSError, ValueError):
            return None
        return rx, tx

    def sample(self) -> tuple[float, float]:
        """Return ``(rx_bps, tx_bps)`` since the previous call."""
        import time

        cur = self._counters()
        now = time.monotonic()
        if cur is None:
            self._prev, self._prev_ts = None, now
            return 0.0, 0.0
        if self._prev is None:
            self._prev, self._prev_ts = cur, now
            return 0.0, 0.0
        elapsed = max(now - self._prev_ts, 1e-6)
        rx = max(0, cur[0] - self._prev[0]) / elapsed
        tx = max(0, cur[1] - self._prev[1]) / elapsed
        self._prev, self._prev_ts = cur, now
        return rx, tx


def online_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0)) or os.cpu_count() or 1
    except AttributeError:
        return os.cpu_count() or 1


def procs_running() -> int:
    """How many tasks are runnable right now (from /proc/stat).

    If this exceeds the number of online CPUs, at least one task is waiting for
    a CPU, which is the cue for the burners to get out of the way.
    """
    try:
        with open("/proc/stat", "r") as handle:
            for line in handle:
                if line.startswith("procs_running"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0
