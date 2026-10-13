"""Command line interface: ``oc-alive <command>``."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__, config as config_mod

STATE_FILE = "status.json"
PAUSE_FILE = "PAUSED"
UNIT = "oc-alive.service"


# --------------------------------------------------------------------- output

def _colour(text: str, ok: bool | None) -> str:
    if not sys.stdout.isatty():
        return text
    code = {True: "32", False: "31", None: "33"}[ok]
    return f"\033[{code}m{text}\033[0m"


def _verdict(value: float | None, threshold: float, enabled: bool = True) -> str:
    if not enabled:
        return _colour("(disabled)", None)
    if value is None:
        return _colour("unknown", None)
    return _colour(
        f"{value:6.1f}%  {'OK' if value >= threshold else 'BELOW ' + str(threshold) + '%'}",
        value >= threshold,
    )


def _status_path() -> Path:
    return config_mod.state_dir() / STATE_FILE


def read_status() -> dict:
    path = _status_path()
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------- commands

def cmd_run(args: argparse.Namespace) -> int:
    from .daemon import Daemon

    path = Path(args.config) if args.config else config_mod.config_path()
    cfg = config_mod.load(path)
    if args.foreground:
        return Daemon(cfg, path).run()
    return Daemon(cfg, path).run()


def cmd_status(args: argparse.Namespace) -> int:
    status = read_status()
    if not status:
        print(f"no status file yet ({_status_path()}) - is {UNIT} running?")
        return 1
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    threshold = float(status.get("oracle_idle_threshold_percent", 20.0))
    target = threshold * config_mod.SAFETY_FACTOR
    cpu = status.get("cpu", {})
    mem = status.get("mem", {})
    net = status.get("net", {})
    print(f"oc-alive {status.get('version')}  pid={status.get('pid')}  "
          f"uptime={_human(status.get('uptime_s', 0))}  "
          f"paused={status.get('paused')}  updated={status.get('updated_at')}")
    print(f"config: {status.get('config_path')}")
    print()
    print(f"Oracle idle bar: below {threshold:.0f}% on CPU, network and memory "
          f"(over 7 days) => reclaimed.  oc-alive targets "
          f"cpu {cpu.get('target_percent')}% / mem {mem.get('target_percent')}% / "
          f"net {net.get('target_percent')}%, verdicts below use the 2x floor "
          f"({target:.0f}%).")
    print()
    print(f"CPU   host util      {_verdict(cpu.get('host_percent'), target, cpu.get('enabled', True))}"
          f"   target={cpu.get('target_percent')}% duty={cpu.get('duty')} "
          f"threads={cpu.get('threads')} sched={cpu.get('scheduler')}/nice{cpu.get('nice')}")
    print(f"MEM   resident      {_verdict(mem.get('used_percent'), target, mem.get('enabled', True))}"
          f"   target={mem.get('target_percent')}% held={mem.get('held_mb')}MiB "
          f"avail={mem.get('available_mb')}MiB")
    link = float(net.get("link_mbps") or 0) or 1.0
    net_percent = (((net.get("rx_mbps") or 0) + (net.get("tx_mbps") or 0)) / link) * 100.0
    print(f"NET   VNIC rx+tx    {_verdict(net_percent, target, net.get('enabled', True))}"
          f"   target={net.get('target_percent')}% mode={net.get('mode')} "
          f"iface={net.get('interface')} rx={net.get('rx_mbps')} tx={net.get('tx_mbps')} "
          f"Mbps of {link:.0f} link (rate={net.get('rate_mbps')}/{net.get('target_mbps')} Mbps)")
    if not mem.get("enabled", True) or not net.get("enabled", True):
        off = [name for name, reg in (("memory", mem), ("network", net))
               if not reg.get("enabled", True)]
        print(f"\n      {' and '.join(off)} tripping is disabled: the idle conditions are "
              f"ANDed, so CPU above the bar is enough on its own.")
    if net.get("enabled", True):
        print(f"      downloaded today: {net.get('downloaded_gib_today')} GiB"
              f" (budget {net.get('budget_gib')} GiB) last={net.get('last_action')}")
    print()
    print(status.get("default_interval_guard", ""))
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    for key, description in config_mod.DESCRIPTIONS.items():
        value = config_mod.get(cfg, key)
        print(f"{key:28s} = {json.dumps(value)[:60]:<60s}  # {description}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    value = config_mod.set_dotted(cfg, args.key, args.value)
    config_mod.save(cfg)
    print(f"{args.key} = {json.dumps(value)}")
    if os.geteuid() != 0:
        print(f"run 'sudo oc-alive set {args.key} {args.value}' to apply")
        return 0
    if args.restart:
        subprocess.run(["systemctl", "restart", UNIT], check=False,
                       stdout=subprocess.DEVNULL)
        print(f"restarted {UNIT}")
        return 0
    result = subprocess.run(["systemctl", "reload", UNIT], check=False)
    if result.returncode == 0:
        print(f"reloaded {UNIT} (soft reload: held memory stays put)")
    else:
        subprocess.run(["systemctl", "restart", UNIT], check=False,
                       stdout=subprocess.DEVNULL)
        print(f"applied by restarting {UNIT}")
    return 0


def cmd_reload(_args: argparse.Namespace) -> int:
    result = subprocess.run(["systemctl", "reload", UNIT], check=False)
    if result.returncode != 0:
        print(f"could not reload {UNIT} (is it running? try: systemctl restart {UNIT})")
        return 1
    print(f"{UNIT} reloaded")
    return 0


def cmd_pause(_args: argparse.Namespace) -> int:
    path = config_mod.state_dir() / PAUSE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    print(f"soft-paused (created {path}); resources are released within a few seconds")
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    path = config_mod.state_dir() / PAUSE_FILE
    try:
        path.unlink()
    except OSError:
        pass
    print("resumed")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from .selftest import run_selftest

    return run_selftest(verbose=not args.quiet)


def _human(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oc-alive",
        description="Keep an OCI Always Free instance out of the idle reclamation list.",
    )
    parser.add_argument("--version", action="version", version=f"oc-alive {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the daemon in the foreground (systemd uses this)")
    run.add_argument("--config", help="path to config.json")
    run.add_argument("--foreground", action="store_true", help="accepted for compatibility")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show what the daemon is doing right now")
    status.add_argument("--json", action="store_true", help="raw status.json")
    status.set_defaults(func=cmd_status)

    keys = sub.add_parser("keys", help="list every tunable with its current value")
    keys.set_defaults(func=cmd_keys)

    reload_cmd = sub.add_parser("reload", help="re-read /etc/oc-alive/config.json")
    reload_cmd.set_defaults(func=cmd_reload)

    setter = sub.add_parser("set", help="change a tunable (dotted key) and reload")
    setter.add_argument("key")
    setter.add_argument("value")
    setter.add_argument("--restart", action="store_true",
                        help="restart the unit instead of a soft reload")
    setter.set_defaults(func=cmd_set)

    pause = sub.add_parser("pause", help="soft pause: release CPU/RAM/network, keep the unit up")
    pause.set_defaults(func=cmd_pause)
    resume = sub.add_parser("resume", help="undo 'pause'")
    resume.set_defaults(func=cmd_resume)

    selftest = sub.add_parser("selftest", help="verify the regulators really reach the target")
    selftest.add_argument("--quiet", action="store_true")
    selftest.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except PermissionError as exc:
        print(f"permission denied: {exc}", file=sys.stderr)
        print("hint: use sudo for commands that write /etc/oc-alive or /var/lib/oc-alive",
              file=sys.stderr)
        return 1
