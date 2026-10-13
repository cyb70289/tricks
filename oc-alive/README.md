# oc-alive

Keeps an OCI **Always Free** compute instance out of Oracle's idle-reclamation
list without disturbing whatever you are actually running on the box.

## What Oracle reclaims

From [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm#compute__idleinstances)
(*Idle Compute Instances → Reclamation of Idle Compute Instances*):

> Oracle will deem virtual machine and bare metal compute instances as idle if,
> during a 7-day period, the following are true:
>
> * CPU utilization for the 95th percentile is less than 20%
> * Network utilization is less than 20%
> * Memory utilization is less than 20% (applies to A1 shapes only)

Three consequences drive the whole design:

1. **The conditions are ANDed.** An instance is reclaimed only if *all three*
   are low. Beating the bar on CPU alone is therefore sufficient - which is why
   **oc-alive trips CPU only by default**.
2. **It is a 95th percentile over 7 days.** Brief dips (your builds, the seconds
   it takes to react) are irrelevant; the sustained level is what counts.
3. A1/Arm (`VM.Standard.A1.Flex`, e.g. 2 OCPU / 12 GB) is also judged on memory.
   AMD `E2.1.Micro` is not.

Default target is **45% CPU** (2.25x the 20% bar). Memory (45%) and network
(45% of link) regulators are included, tested, and **off by default**:

```bash
sudo oc-alive set mem.enabled true     # hold ~45% RAM as anonymous pages
sudo oc-alive set net.enabled true     # pull ~9.7 TB/day of ingress (see below)
```

## Install

```bash
cd /home/ubuntu/tricks/oc-alive
sudo ./install.sh              # install + enable + start + print status
sudo ./install.sh --no-start   # copy files only
```

Copies the code to `/opt/oc-alive`, creates a `oc-alive` system user, writes
`/etc/oc-alive/config.json`, installs `oc-alive.service` and the `oc-alive` CLI.
Nothing outside `/opt/oc-alive`, `/etc/oc-alive`, `/var/lib/oc-alive`,
`/usr/local/bin/oc-alive` and `/etc/systemd/system/oc-alive.service` is touched.
Requirements: systemd, `python3 >= 3.10`, no third-party packages.

Re-running `install.sh` restarts the unit so new code is loaded (`--no-start`
does not restart a running unit). `sudo ./uninstall.sh` removes everything.

## Verify

```bash
oc-alive status                 # live view with OK / BELOW verdicts
oc-alive status --json          # machine readable
sudo oc-alive selftest          # exercises every regulator for real, ~45s
journalctl -u oc-alive -f
```

```
CPU   host util        45.3%  OK   target=45.0% duty=0.44 threads=2 sched=idle/nice19
MEM   resident      (disabled)   target=45.0% held=0.0MiB avail=11107.9MiB
NET   VNIC rx+tx    (disabled)   target=45.0% mode=external iface=enp0s6 rx=0.0 tx=0.0 Mbps

      memory and network tripping is disabled: the idle conditions are ANDed, so
      CPU above the bar is enough on its own.
```

On this host (2 OCPU / 12 GB A1) the CPU regulator holds the target steadily -
2s buckets over 60s: min 38%, p5 39%, median 47%, max 63%, **0 buckets below the
20% bar**, so the 95th percentile Oracle samples is around 50%.

## How it stays out of your way

Four independent layers, all of which have to agree before oc-alive burns CPU:

| Layer | Effect |
|---|---|
| `CPUWeight=1` | the cgroup is entitled to ~1% of *contended* CPU. This is the strongest lever - it was worth more than the burners' nice level |
| `SCHED_IDLE` burners (+ `Nice=19`) | the burners only run when nothing else wants the CPU |
| `yield_when_contended` | each burner re-reads `/proc/stat` `procs_running` at the start of every slice and every 50ms during it; if runnable tasks outnumber CPUs it abandons the slice immediately. Reacts in ~50ms, not on the next control sample |
| feedback controller on `/proc/stat` | your own CPU work counts toward the target, so the requested duty drops to 0 all by itself |

Plus: `IOSchedulingClass=idle` and no disk writes at all (nothing to wear out);
`MemoryMax=55%` + `OOMScoreAdjust=800` so oc-alive can never OOM the rest of the
host and is the first thing killed if anything goes wrong; `oc-alive pause`
releases everything within ~4s and keeps the unit running.

### Measured interference

Two normal-priority processes saturating both cores, alternating A/B with the
service active and soft-paused, measuring `/proc/stat` and the service's
`cpu.stat` (8s each):

| | user processes got | oc-alive's cgroup used |
|---|---|---|
| active | 15.88s / 15.87s / 15.89s of 16.0s | 0.01s / 0.01s / 0.02s |
| paused | 15.90s / 15.86s / 15.88s of 16.0s | 0.01s / 0.01s / 0.01s |

**Indistinguishable.** The built-in selftest asserts the same thing: a nice-0
foreground loop keeps 99.5% of its core while the burners run at 60%.

Getting there needed two fixes, both now in the defaults: `CPUWeight` was 10
(the cgroup, not the burners, was the dominant cost - it took up to 1.3s of CPU
per 8s window), and the burners only reacted to contention on the next
controller sample (~1.5s late). With the contention guard the reaction is ~50ms.

### If you enable the memory regulator

It claims anonymous pages (`MAP_PRIVATE`, page-touched, so they show up as
*used* memory) and releases them the moment they would get in your way:
`MemAvailable` floor (1536 MiB) and PSI memory pressure (`some avg10 > 2%` or
`full avg10 > 0.5%`). Verified: a user process touched 6 GiB in 2.4s, oc-alive
dropped from 4800 MiB held to 64 MiB (`released:psi-full(0.60)`), then refilled
once the memory was freed. It also only ever fills the *gap* - if you already use
40%, it holds almost nothing.

## Tuning

```bash
oc-alive keys                              # every knob, current value, short help
sudo oc-alive set cpu.target_percent 50
sudo oc-alive set net.enabled false
sudo oc-alive reload                       # after hand-editing config.json
```

`set` rewrites `/etc/oc-alive/config.json` and sends `systemctl reload` (SIGHUP),
re-reading the config and re-targeting the running regulators without dropping
held memory; `--restart` forces a full restart. Regulators whose `enabled` flag
is false are **not started at all** - no threads, no sockets, no polling - and
turning them on later starts them live. `cpu.threads` is the one setting that
needs a restart (a warning is logged).

### Network, if you turn it on

`mode = "external"`, `target_percent = 45`, `link_mbps = 0` (auto = OCPUs x 1000)
means **900 Mbit/s ≈ 9.7 TB/day of ingress** on a 2 OCPU instance. Ingress is not
billed on OCI and does not count against the 10 TB/month egress allowance
(egress is ~1 Mbit/s of ACKs), but it is a lot of traffic - and because the
conditions are ANDed it buys you nothing while CPU is above the bar.

```bash
sudo oc-alive set net.target_percent 10    # 200 Mbit/s, ~2 TB/day
sudo oc-alive set net.daily_budget_gb 100  # stop generating after 100 GiB/day
sudo oc-alive set net.mode loopback        # CPU burn on 127.0.0.1 only, no WAN use
```

Generation is deficit-only (it measures `rx+tx` and adds only what is missing,
so your own traffic counts), stops above `cpu_guard_percent` (80%), scores
mirrors by observed throughput and skips failing ones with exponential backoff,
and falls back to a local `127.0.0.1` sink when every mirror is unreachable.

## Layout

```
/opt/oc-alive/oc_alive/            code (python, stdlib only)
/etc/oc-alive/config.json          tunables
/var/lib/oc-alive/status.json      live status (world readable)
/var/lib/oc-alive/PAUSED           soft-pause flag
/etc/systemd/system/oc-alive.service
/usr/local/bin/oc-alive            CLI wrapper
```

## Development

```bash
python3 -m unittest discover -s tests -v          # 17 pure-logic tests
PYTHONPATH=src python3 -m oc_alive selftest       # real load, ~45s
PYTHONPATH=src python3 -m oc_alive status
```

| Module | Responsibility |
|---|---|
| `metrics.py` | `/proc/stat` (incl. `procs_running`), `/proc/meminfo`, `/proc/pressure/memory`, VNIC counters |
| `cpu.py` | feedback controller, duty-cycled `hashlib` burners (OpenSSL releases the GIL, so N threads really use N cores), contention guard |
| `mem.py` | anonymous `mmap` blocks (`MAP_PRIVATE`, page-touched), released under pressure |
| `net.py` | token bucket, throughput-scored mirrors, HTTP download-to-`/dev/null`, loopback sink |
| `daemon.py` | lifecycle, SIGHUP reload, lazy start/stop of disabled regulators, status file, systemd notify/watchdog |
| `config.py` | defaults, deep merge, dotted-key get/set with type coercion |
| `selftest.py` | proves each regulator reaches its target, baseline-aware, including the non-interference check |

## Caveats

* Oracle does not document how the hypervisor measures "network utilisation"
  (direction, or what 20% is 20% *of*). `link_mbps` is an assumption (OCPUs x
  1000 for A1.Flex); CPU is the metric that is fully reproducible from inside the
  guest, which is another reason it is the only one enabled by default.
* `yield_when_contended` means background daemon activity on an otherwise idle
  box can briefly pause the burners, so a 2s sample can read as low as ~38%.
  1-minute samples (what a 7-day 95th percentile is built from) average ~48%.
* Keeping an instance busy is not a licence to abuse free resources. The CPU-only
  default is deliberately modest; enabling the network regulator is your call.
