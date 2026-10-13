"""``oc-alive selftest`` - prove the regulators really reach their targets.

Runs each regulator in-process for a few seconds and measures the *host* effect
through procfs, so it works the same way before and after installation.

The checks are baseline aware: oc-alive only ever fills the *gap* up to its
target, so if the machine is already above the target (because the service is
already running, or because you are busy) the selftest verifies the
deficit-filling behaviour instead of demanding that it add more load.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import config as config_mod, metrics
from .cpu import CpuRegulator
from .mem import MemRegulator
from .net import NetRegulator
from .priority import apply_priority, priority_label

MiB = 1024 * 1024
MARGIN = 5.0  # percent of slack allowed on a measurement


class Checker:
    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self.failures = 0
        self.checks = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks += 1
        if not ok:
            self.failures += 1
        flag = "PASS" if ok else "FAIL"
        if self.verbose or not ok:
            print(f"  [{flag}] {name}" + (f" - {detail}" if detail else ""))

    def info(self, text: str) -> None:
        if self.verbose:
            print(f"         {text}")


# --------------------------------------------------------------------- helpers

def _host_cpu_percent(duration: float) -> float:
    busy0, total0 = metrics.cpu_snapshot()
    time.sleep(duration)
    busy1, total1 = metrics.cpu_snapshot()
    delta = total1 - total0
    return (busy1 - busy0) / delta * 100.0 if delta > 0 else 0.0


def _self_cpu_seconds() -> float:
    fields = Path("/proc/self/stat").read_text().split()
    ticks = os.sysconf("SC_CLK_TCK")
    return (int(fields[13]) + int(fields[14])) / ticks


def _foreground_busy(seconds: float) -> tuple[float, float]:
    """Burn CPU in the *calling* thread at its current priority."""
    started = time.monotonic()
    cpu0 = _self_cpu_seconds()
    deadline = started + seconds
    while time.monotonic() < deadline:
        pass
    return _self_cpu_seconds() - cpu0, time.monotonic() - started


def _used_percent() -> float:
    info = metrics.meminfo()
    total = info.get("MemTotal", 0)
    return metrics.mem_used(info) / total * 100.0 if total else 0.0


# ------------------------------------------------------------------------ test

def run_selftest(verbose: bool = True) -> int:
    check = Checker(verbose)
    info = metrics.meminfo()
    print(f"oc-alive selftest on {os.uname().machine}, {metrics.online_cpus()} CPUs, "
          f"{info.get('MemTotal', 0) / MiB:,.0f} MiB RAM, "
          f"{metrics.default_interface() or 'no'} interface")

    # 0. priority plumbing -------------------------------------------------
    print("[0] priority / scheduler")
    probe: dict[str, str] = {}

    def _probe() -> None:
        apply_priority("batch", 19)
        probe["label"] = priority_label()

    thread = threading.Thread(target=_probe)
    thread.start()
    thread.join()
    check.check("scheduler drops to batch/idle nice19", "nice19" in probe.get("label", ""),
                probe.get("label", "?"))

    # 1. config round trip ------------------------------------------------
    print("[1] configuration")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = config_mod.load(path)
        bar = config_mod.IDLE_THRESHOLD_PERCENT * config_mod.SAFETY_FACTOR
        check.check("every default target is at least 2x the idle bar",
                    all(cfg[k]["target_percent"] >= bar for k in ("cpu", "mem", "net")),
                    f"cpu={cfg['cpu']['target_percent']} mem={cfg['mem']['target_percent']} "
                    f"net={cfg['net']['target_percent']} (bar={config_mod.IDLE_THRESHOLD_PERCENT})")
        config_mod.set_dotted(cfg, "net.target_percent", "12.5")
        config_mod.set_dotted(cfg, "cpu.enabled", "false")
        config_mod.save(cfg, path)
        again = config_mod.load(path)
        check.check("set/save/load round trip with type coercion",
                    again["net"]["target_percent"] == 12.5
                    and again["cpu"]["enabled"] is False,
                    json.dumps({"net.target_percent": again["net"]["target_percent"],
                                "cpu.enabled": again["cpu"]["enabled"]}))

    # 2. CPU --------------------------------------------------------------
    target = 40.0
    print(f"[2] CPU regulator (target {target:.0f}%, 8s)")
    baseline = _host_cpu_percent(2.0)
    cpu = CpuRegulator({"enabled": True, "target_percent": target, "max_percent": 80.0,
                        "control_interval_ms": 1000, "cycle_ms": 200,
                        "gain_up": 0.6, "gain_down": 0.35, "jitter_percent": 15.0})
    cpu.start()
    time.sleep(3.0)
    measured = _host_cpu_percent(5.0)
    duty = cpu.duty
    cpu.stop()
    reached = measured >= target - MARGIN
    check.check(f"host CPU reaches {target:.0f}% (2x the 20% idle bar)", reached,
                f"baseline {baseline:.1f}% -> measured {measured:.1f}% "
                f"(own duty {duty:.2f}, {cpu.threads} threads)")
    if not reached and baseline >= target - MARGIN:
        check.check("host CPU was already above target without us", True,
                    "deficit-filling: nothing extra to add")

    # 3. memory -----------------------------------------------------------
    target = 30.0
    print(f"[3] memory regulator (target {target:.0f}%)")
    anon_before = metrics.meminfo().get("AnonPages", 0)
    shmem_before = metrics.meminfo().get("Shmem", 0)
    probe_reg = MemRegulator({"enabled": False})
    try:
        check.check("can claim anonymous pages", probe_reg._grow(32 * MiB),  # noqa: SLF001
                    "32 MiB requested")
        after = metrics.meminfo()
        anon_delta = after.get("AnonPages", 0) - anon_before
        shmem_delta = after.get("Shmem", 0) - shmem_before
        check.check("claimed pages count as used memory (not cache/shmem)",
                    anon_delta >= 16 * MiB and shmem_delta < 8 * MiB,
                    f"AnonPages +{anon_delta / MiB:.0f} MiB, Shmem +{shmem_delta / MiB:.0f} MiB")
        freed = probe_reg._release(64 * MiB)  # noqa: SLF001
        check.check("pages are given back on release", freed >= 32 * MiB and probe_reg._held == 0,  # noqa: SLF001
                    f"released {freed / MiB:.0f} MiB")
    finally:
        probe_reg.stop()

    baseline_used = _used_percent()
    mem = MemRegulator({"enabled": True, "target_percent": target, "max_percent": 35.0,
                        "control_interval_ms": 1000, "grow_chunks_per_cycle": 8,
                        "chunk_mb": 64})
    mem.start()
    time.sleep(10.0)
    snapshot = mem.snapshot()
    mem.stop()
    used = snapshot["used_percent"]
    ok = used >= target - MARGIN or baseline_used >= target - MARGIN
    check.check(f"host memory reaches {target:.0f}% (2x the 20% idle bar)", ok,
                f"baseline {baseline_used:.1f}% -> used {used:.1f}%, "
                f"held by this regulator {snapshot['held_mb']:.0f} MiB, "
                f"MemAvailable {snapshot['available_mb']:.0f} MiB")

    # 4. network ----------------------------------------------------------
    print("[4] network regulator (loopback mode, 150 Mbit/s, 6s)")
    net = NetRegulator({"enabled": True, "mode": "loopback", "target_percent": 100.0,
                        "link_mbps": 150.0, "max_mbps": 150.0, "workers": 1,
                        "control_interval_ms": 1000, "chunk_kib": 256})
    net.start()
    time.sleep(1.0)
    before = net.loopback_bytes
    time.sleep(5.0)
    moved = net.loopback_bytes - before
    snapshot = net.snapshot()
    net.stop()
    measured_mbps = moved * 8 / 5.0 / 1_000_000
    check.check("loopback sink moves data at the requested rate", measured_mbps >= 75.0,
                f"measured {measured_mbps:.0f} Mbit/s (configured {snapshot['rate_mbps']:.0f}), "
                f"{snapshot['loopback_mib']:.0f} MiB sent")

    # 5. do not disturb ---------------------------------------------------
    print("[5] does not disturb: nice-0 foreground workload while burners run at 60%")
    burners = CpuRegulator({"enabled": True, "target_percent": 60.0, "max_percent": 80.0,
                            "control_interval_ms": 1000, "cycle_ms": 200,
                            "gain_up": 0.8, "gain_down": 0.35, "jitter_percent": 15.0})
    burners.start()
    time.sleep(3.0)
    busy_cpu, busy_wall = _foreground_busy(6.0)
    burners.stop()
    share = busy_cpu / busy_wall * 100.0 if busy_wall else 0.0
    check.check("foreground workload keeps its core", share >= 90.0,
                f"got {busy_cpu:.2f}s CPU in {busy_wall:.2f}s wall = {share:.1f}% of a core")

    print()
    if check.failures:
        print(f"SELFTEST FAILED: {check.failures}/{check.checks} checks failed")
        return 1
    print(f"SELFTEST OK: {check.checks}/{check.checks} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run_selftest())
