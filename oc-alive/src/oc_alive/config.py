"""Configuration loading / saving with dotted key access."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "/etc/oc-alive/config.json"
DEFAULT_STATE_DIR = "/var/lib/oc-alive"

#: Oracle's published idle thresholds (percent), and the factor we aim for.
IDLE_THRESHOLD_PERCENT = 20.0
SAFETY_FACTOR = 2.0

DEFAULTS: dict[str, Any] = {
    "version": 1,
    # Soft switch: when true every regulator releases its resources.
    "paused": False,
    "log_interval_s": 60,
    "status_interval_s": 5,
    "cpu": {
        "enabled": True,
        # Aim for 45% (2.25x the 20% idle bar). /proc/stat based feedback keeps
        # the *host* utilisation at this level whatever else is running.
        "target_percent": 45.0,
        "max_percent": 75.0,
        # 0 = one burner per online CPU.
        "threads": 0,
        "cycle_ms": 500,
        "control_interval_ms": 1500,
        # Aggressive on the way up (never look idle), gentle on the way down.
        "gain_up": 1.1,
        "gain_down": 0.4,
        "jitter_percent": 15.0,
        # Instantly drop the current burn slice when more tasks are runnable than
        # there are CPUs - the fastest possible "get out of the way".
        "yield_when_contended": True,
        "contended_check_ms": 50,
        # SCHED_IDLE burners yield to *any* runnable task: zero measurable cost
        # when you are using the box, full CPU when you are not.
        "scheduler": "idle",
        "nice": 19,
    },
    "mem": {
        # Off by default: Oracle reclaims only when CPU *and* network *and*
        # memory are all below 20%, so keeping CPU high is sufficient on its own.
        # Turn on with: sudo oc-alive set mem.enabled true
        "enabled": False,
        "target_percent": 45.0,
        "max_percent": 55.0,
        "chunk_mb": 64,
        "control_interval_ms": 2000,
        "grow_chunks_per_cycle": 6,
        # Hard floors: below these the regulator gives memory back immediately.
        "min_available_mb": 1536,
        "psi_some_limit": 2.0,
        "psi_full_limit": 0.5,
        "retouch_interval_s": 300,
    },
    "net": {
        # Off by default (see mem.enabled above).  Enabling "external" pulls a
        # lot of traffic: 45% of a 2 Gbps A1 link is ~9.7 TB/day of ingress.
        "enabled": False,
        # "external" downloads through the VNIC (counts for OCI's metric),
        # "loopback" only burns CPU via 127.0.0.1 (never counts, no WAN use).
        "mode": "external",
        # "" = auto detect from the default route.
        "interface": "",
        # 0 = auto (online CPUs * 1000 Mbit/s, the A1.Flex rule).
        "link_mbps": 0,
        "target_percent": 45.0,
        "max_mbps": 950.0,
        "workers": 2,
        "control_interval_ms": 2000,
        "gain": 1.0,
        # 0 = unlimited. Ingress is not billed on OCI, but you can cap it.
        "daily_budget_gb": 0.0,
        "fallback_loopback": True,
        # Stop generating traffic while the host is already busy.
        "cpu_guard_percent": 80.0,
        "chunk_kib": 256,
        "timeout_s": 20,
        "mirrors": [
            "http://speedtest.singapore.linode.com/100MB-singapore.bin",
            "http://speed.cloudflare.com/__down?bytes=50000000",
            "https://speed.cloudflare.com/__down?bytes=50000000",
            "http://ipv4.download.thinkbroadband.com/1GB.zip",
            "https://ash-speed.hetzner.com/1GB.bin",
            "http://speedtest.newark.linode.com/1GB-newark.bin",
        ],
    },
}

#: Short help strings, used by ``oc-alive keys``.
DESCRIPTIONS: dict[str, str] = {
    "paused": "soft pause: release everything, keep the service running",
    "log_interval_s": "seconds between summary log lines",
    "status_interval_s": "seconds between status.json refreshes",
    "cpu.enabled": "enable the CPU regulator",
    "cpu.target_percent": "target host CPU utilisation (2x idle bar = 40)",
    "cpu.max_percent": "upper clamp for the CPU target",
    "cpu.threads": "burner threads (0 = one per online CPU; needs a restart)",
    "cpu.cycle_ms": "duty-cycle length; each cycle burns target*duty then sleeps",
    "cpu.control_interval_ms": "feedback sampling window",
    "cpu.gain_up": "controller gain when below target (recovery speed)",
    "cpu.gain_down": "controller gain when above target (back-off speed)",
    "cpu.jitter_percent": "random duty-cycle jitter, so the load is not a flat line",
    "cpu.yield_when_contended": "stop burning mid-slice as soon as tasks outnumber CPUs",
    "cpu.contended_check_ms": "how often the burners check for contention",
    "cpu.scheduler": "scheduler class for burners: idle (yields to everything) | batch | other",
    "cpu.nice": "nice level for burners",
    "mem.enabled": "enable the memory regulator (off: CPU alone beats the AND condition)",
    "mem.target_percent": "target host memory utilisation",
    "mem.max_percent": "hard cap for memory held by oc-alive",
    "mem.chunk_mb": "allocation granularity",
    "mem.grow_chunks_per_cycle": "how fast memory is claimed (chunks per sample)",
    "mem.min_available_mb": "give memory back below this MemAvailable floor",
    "mem.psi_some_limit": "release memory when PSI memory 'some avg10' exceeds this",
    "mem.psi_full_limit": "release memory when PSI memory 'full avg10' exceeds this",
    "mem.retouch_interval_s": "seconds between page re-touch passes",
    "net.enabled": "enable the network regulator (off by default: it pulls a lot of traffic)",
    "net.mode": "external (through the VNIC) or loopback",
    "net.interface": "interface to measure ('' = default route)",
    "net.link_mbps": "assumed link speed (0 = CPUs * 1000)",
    "net.target_percent": "target VNIC utilisation",
    "net.max_mbps": "hard rate cap",
    "net.workers": "parallel download workers",
    "net.gain": "integral controller gain for the traffic rate",
    "net.daily_budget_gb": "stop external traffic after this many GiB per day (0 = unlimited)",
    "net.fallback_loopback": "keep burning CPU locally when mirrors are unreachable",
    "net.cpu_guard_percent": "stop generating traffic when host CPU is above this",
    "net.chunk_kib": "read/send size",
    "net.timeout_s": "per request socket timeout",
    "net.mirrors": "download sources, rotated automatically",
}


def config_path() -> Path:
    return Path(os.environ.get("OC_ALIVE_CONFIG", DEFAULT_CONFIG_PATH))


def state_dir() -> Path:
    return Path(os.environ.get("OC_ALIVE_STATE_DIR", DEFAULT_STATE_DIR))


def deep_merge(base: dict, override: dict) -> dict:
    """Return ``base`` deep-merged with ``override`` (override wins)."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load(path: Path | None = None) -> dict:
    """Load the config file, filling in any missing default."""
    path = Path(path) if path else config_path()
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return deep_merge(DEFAULTS, raw)


def save(cfg: dict, path: Path | None = None) -> None:
    path = Path(path) if path else config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(cfg, indent=2, sort_keys=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get(cfg: dict, dotted: str, default: Any = None) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def coerce(current: Any, raw: str) -> Any:
    """Turn a CLI string into the type of the value it replaces."""
    if isinstance(current, bool):
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on", "y"):
            return True
        if low in ("0", "false", "no", "off", "n"):
            return False
        raise ValueError(f"expected a boolean, got {raw!r}")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        raw = raw.strip()
        if raw.startswith("["):
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError("expected a JSON list")
            return parsed
        return [item for item in (p.strip() for p in raw.split(",")) if item]
    if current is None:
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def set_dotted(cfg: dict, dotted: str, raw: str) -> Any:
    """Set ``dotted`` in ``cfg`` from the string ``raw``; returns the new value."""
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    current = node.get(parts[-1])
    value = coerce(current, raw)
    node[parts[-1]] = value
    return value


def idle_target(percent_key: str, cfg: dict) -> float:
    """Helper: 2x the published idle threshold for a given resource."""
    return max(
        float(get(cfg, percent_key, IDLE_THRESHOLD_PERCENT * SAFETY_FACTOR)),
        IDLE_THRESHOLD_PERCENT * SAFETY_FACTOR,
    )
