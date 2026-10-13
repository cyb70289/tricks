"""Priority helpers + a tiny sd_notify implementation.

Everything oc-alive does must be *background* work: it has to be the first
thing to give way when the owner of the instance does something real.
"""

from __future__ import annotations

import logging
import os
import socket
import time

log = logging.getLogger("oc-alive.priority")

_SCHED = {
    "other": os.SCHED_OTHER,
    "batch": getattr(os, "SCHED_BATCH", os.SCHED_OTHER),
    "idle": getattr(os, "SCHED_IDLE", os.SCHED_OTHER),
}


def apply_priority(policy: str = "batch", nice: int = 19) -> None:
    """Push the calling thread to the back of the queue (best effort)."""
    if nice:
        try:
            os.nice(int(nice) - os.nice(0))
        except OSError as exc:  # pragma: no cover - depends on privileges
            log.debug("nice() failed: %s", exc)
    sched = _SCHED.get(str(policy).lower())
    if sched is None:
        return
    try:
        os.sched_setscheduler(0, sched, os.sched_param(0))
    except OSError as exc:  # pragma: no cover - depends on kernel
        log.debug("sched_setscheduler(%s) failed: %s", policy, exc)


def priority_label() -> str:
    """Human readable description of our own scheduling class."""
    names = {os.SCHED_OTHER: "other", getattr(os, "SCHED_BATCH", -1): "batch",
             getattr(os, "SCHED_IDLE", -2): "idle", os.SCHED_FIFO: "fifo",
             os.SCHED_RR: "rr"}
    try:
        policy = os.sched_getscheduler(0)
    except OSError:
        policy = os.SCHED_OTHER
    try:
        nice = os.nice(0)
    except OSError:
        nice = 0
    return f"{names.get(policy, policy)}/nice{nice}"


def notify(state: str) -> None:
    """Send a message to systemd's notify socket (no-op if not running)."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.connect(address)
        sock.sendall(state.encode("utf-8"))
    except OSError as exc:
        log.debug("sd_notify(%s) failed: %s", state, exc)
    finally:
        sock.close()


def watchdog_ping() -> None:
    notify("WATCHDOG=1")


def monotonic() -> float:
    return time.monotonic()
