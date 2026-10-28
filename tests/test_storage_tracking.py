"""Behavioral checks: wrong time windows, false recoveries, PID reuse, daily deltas."""

import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import storage_tracking as s

TEMP = Path(__file__).resolve().parents[2] / "test-tmp"
TEMP.mkdir(exist_ok=True)


class AlarmTests(unittest.TestCase):
    def test_burst_seen_against_recent_peak_not_just_previous_sample(self):
        # Removing the in-window peak check misses a burst split over many samples.
        self.assertIn(
            "fast-fall",
            s.reasons(
                [(0, 40_000_000_000), (200, 39_000_000_000), (400, 37_900_000_000)],
                400,
                37_900_000_000,
                10_000_000_000,
            ),
        )

    def test_stale_peak_does_not_alarm(self):
        self.assertNotIn(
            "fast-fall",
            s.reasons(
                [(0, 40_000_000_000), (601, 35_000_000_000)],
                601,
                35_000_000_000,
                10_000_000_000,
            ),
        )

    def test_unavailable_measurement_never_reports_recovery(self):
        self.assertEqual(s.reasons([(0, 5)], 5, None, 10), ["unavailable"])

    def test_new_fast_fall_captures_even_during_existing_low_space_cooldown(self):
        self.assertTrue(
            s.capture_due(["low-space"], ["low-space", "fast-fall"], False, 5, 0)
        )

    def test_unchanged_alarm_is_bounded_by_cooldown(self):
        self.assertFalse(
            s.capture_due(
                ["low-space", "fast-fall"], ["low-space", "fast-fall"], False, 5, 0
            )
        )

    def test_pid_reuse_is_not_attributed_to_old_process(self):
        old = [{"pid": 7, "start": 1, "written": 100, "name": "old"}]
        now = [{"pid": 7, "start": 2, "written": 999, "name": "new"}]
        self.assertEqual(s.writer_deltas(old, now), [])

    def test_writer_delta_not_lifetime_bytes(self):
        old = [{"pid": 7, "start": 1, "written": 1000, "name": "writer"}]
        now = [{"pid": 7, "start": 1, "written": 1123, "name": "writer"}]
        self.assertEqual(s.writer_deltas(old, now)[0]["delta_bytes"], 123)

    def test_failed_directory_scan_not_reported_as_zero_growth(self):
        now = {"A": {"bytes": None}, "B": {"bytes": 130}, "C": {"bytes": 20}}
        old = {"A": {"bytes": 100}, "B": {"bytes": 100}}
        self.assertEqual(s.daily_changes(now, old), [{"root": "B", "delta_bytes": 30}])

    @unittest.skipUnless(Path("/bin/zsh").exists(), "requires zsh config evaluation")
    def test_sources_toolkit_with_required_root_and_uses_profile_interval(self):
        # Leaving PT_ROOT unset made the real config refuse to load in the native smoke.
        with tempfile.TemporaryDirectory(dir=TEMP, prefix="toolkit with spaces ") as d:
            p = Path(d)
            (p / "config.sh").write_text(
                ": ${PT_ROOT:?required}\nMEM_DISK_LOW_BYTES=12345\nMEM_WATCHDOG_INTERVAL_SEC=120\n"
            )
            self.assertEqual(s.config_values(p), {"low": 12345, "memory_interval": 120})

    def test_reports_compare_prior_day_not_same_day_rerun(self):
        with tempfile.TemporaryDirectory(
            dir=TEMP,
        ) as d:
            p = Path(d)
            (p / "2026-10-16.json").write_text("{}")
            (p / "2026-10-17.json").write_text("{}")
            (p / "2026-10-18.json").write_text("{}")
            self.assertEqual(s.previous_day(p, "2026-10-18").name, "2026-10-17.json")


if __name__ == "__main__":
    unittest.main()
