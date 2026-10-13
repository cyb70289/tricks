"""Unit tests for the pure logic (no root, no real load).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oc_alive import config as config_mod  # noqa: E402
from oc_alive import metrics  # noqa: E402
from oc_alive.net import MirrorPool, RateLimiter  # noqa: E402

MiB = 1024 * 1024


class TestConfig(unittest.TestCase):
    def test_defaults_exceed_twice_the_idle_bar(self) -> None:
        cfg = config_mod.load(Path("/nonexistent/config.json"))
        for key in ("cpu", "mem", "net"):
            self.assertGreaterEqual(
                cfg[key]["target_percent"],
                config_mod.IDLE_THRESHOLD_PERCENT * config_mod.SAFETY_FACTOR,
                key,
            )

    def test_unknown_keys_are_kept_and_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"cpu": {"target_percent": 55}, "extra": 1}))
            cfg = config_mod.load(path)
            self.assertEqual(cfg["cpu"]["target_percent"], 55)
            self.assertEqual(cfg["cpu"]["max_percent"], config_mod.DEFAULTS["cpu"]["max_percent"])
            self.assertEqual(cfg["extra"], 1)

    def test_coercion(self) -> None:
        self.assertIs(config_mod.coerce(True, "no"), False)
        self.assertEqual(config_mod.coerce(1, "7"), 7)
        self.assertEqual(config_mod.coerce(1.5, "2.25"), 2.25)
        self.assertEqual(config_mod.coerce("x", "y"), "y")
        self.assertEqual(config_mod.coerce([], "a,b , c"), ["a", "b", "c"])
        with self.assertRaises(ValueError):
            config_mod.coerce(True, "maybe")

    def test_set_dotted_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            cfg = config_mod.load(path)
            config_mod.set_dotted(cfg, "net.mirrors", '["http://a/1"]')
            config_mod.set_dotted(cfg, "mem.chunk_mb", "32")
            config_mod.save(cfg, path)
            again = config_mod.load(path)
            self.assertEqual(again["net"]["mirrors"], ["http://a/1"])
            self.assertEqual(again["mem"]["chunk_mb"], 32)


class TestMetrics(unittest.TestCase):
    def test_cpu_meter_is_sane(self) -> None:
        meter = metrics.CpuMeter()
        time.sleep(0.3)
        value = meter.sample()
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 100.0 * max(1, metrics.online_cpus()))

    def test_meminfo(self) -> None:
        info = metrics.meminfo()
        self.assertGreater(info["MemTotal"], 0)
        self.assertLessEqual(metrics.mem_used(info), info["MemTotal"])

    def test_default_interface_is_a_string(self) -> None:
        self.assertIsInstance(metrics.default_interface(), str)

    def test_net_meter(self) -> None:
        meter = metrics.NetMeter(metrics.default_interface())
        meter.sample()
        rx, tx = meter.sample()
        self.assertGreaterEqual(rx, 0.0)
        self.assertGreaterEqual(tx, 0.0)


class TestMemoryAccounting(unittest.TestCase):
    def test_regulator_uses_private_anonymous_pages(self) -> None:
        """Regression: mmap must not use the MAP_SHARED default (shmem).

        Shmem pages are real RAM, but the guest reports them as ``Cached``,
        so the box looks idle in its own accounting while holding gigabytes.
        """
        from oc_alive.mem import MemRegulator

        size = 16 * MiB
        before = metrics.meminfo()
        regulator = MemRegulator({"enabled": False})
        try:
            self.assertTrue(regulator._grow(size))  # noqa: SLF001
            after = metrics.meminfo()
        finally:
            regulator.stop()
        anon_growth = after.get("AnonPages", 0) - before.get("AnonPages", 0)
        shmem_growth = after.get("Shmem", 0) - before.get("Shmem", 0)
        self.assertGreaterEqual(anon_growth, size // 2, "pages are not anonymous")
        self.assertLess(shmem_growth, size // 4, "pages landed in shmem/page cache")

    def test_release_returns_memory(self) -> None:
        from oc_alive.mem import MemRegulator

        regulator = MemRegulator({"enabled": False})
        try:
            regulator._grow(8 * MiB)  # noqa: SLF001
            self.assertEqual(regulator._held, 8 * MiB)  # noqa: SLF001
            self.assertEqual(regulator._release(64 * MiB), 8 * MiB)  # noqa: SLF001
            self.assertEqual(regulator._held, 0)  # noqa: SLF001
        finally:
            regulator.stop()


class TestRateLimiter(unittest.TestCase):
    def test_zero_rate_blocks(self) -> None:
        limiter = RateLimiter()
        stop = threading.Event()
        stop.set()
        self.assertFalse(limiter.pay(1, stop))

    def test_rate_is_enforced(self) -> None:
        limiter = RateLimiter()
        limiter.set_rate(100_000)  # 100 kB/s
        stop = threading.Event()
        start = time.monotonic()
        for _ in range(5):
            self.assertTrue(limiter.pay(50_000, stop))
        elapsed = time.monotonic() - start
        # 250 kB at 100 kB/s, minus the 1s burst allowance -> ~1.5s
        self.assertGreater(elapsed, 1.0)
        self.assertLess(elapsed, 3.0)


class TestMirrorPool(unittest.TestCase):
    def test_failed_mirror_is_skipped_then_recovers(self) -> None:
        pool = MirrorPool(["http://a/1", "http://b/2"])
        pool.failed("http://a/1")
        for _ in range(5):
            self.assertEqual(pool.next(), "http://b/2")
        self.assertFalse(pool.all_down())
        pool.failed("http://b/2")
        self.assertTrue(pool.all_down())
        self.assertIsNone(pool.next())
        pool.ok("http://a/1")
        self.assertEqual(pool.next(), "http://a/1")

    def test_fast_mirror_is_preferred_and_slots_are_spread(self) -> None:
        pool = MirrorPool(["http://slow/1", "http://fast/2"])
        pool.done("http://fast/2", 6 * MiB, 1.0)  # observed 6 MiB/s
        self.assertEqual(pool.next(), "http://fast/2")
        self.assertEqual(pool.next(), "http://fast/2")
        self.assertEqual(pool.next(), "http://fast/2")
        # by now the fast mirror has 3 in-flight slots, so the slow one is used
        self.assertEqual(pool.next(), "http://slow/1")

    def test_slow_result_lowers_the_score(self) -> None:
        pool = MirrorPool(["http://a/1", "http://b/2"])
        pool.done("http://a/1", 32 * MiB, 8.0)  # only 4 MiB/s
        pool.done("http://b/2", 64 * MiB, 1.0)  # 64 MiB/s
        for _ in range(3):
            self.assertEqual(pool.next(), "http://b/2")

    def test_done_releases_inflight(self) -> None:
        pool = MirrorPool(["http://only/1"])
        self.assertEqual(pool.next(), "http://only/1")
        pool.done("http://only/1", 0, 0.0)
        self.assertEqual(pool.next(), "http://only/1")

    def test_empty_pool(self) -> None:
        self.assertIsNone(MirrorPool([]).next())
        self.assertFalse(MirrorPool([]).all_down())


if __name__ == "__main__":
    unittest.main()
