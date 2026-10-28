"""Contracts that catch stock-plist reversion, false allocation and lost ancestry."""

import json
import os
import shutil
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT.parent / "test-tmp"
TEMP.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT / "libexec"))
import storage_tracking as s  # noqa: E402 — local libexec path bootstrap


class PortTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin" and os.environ.get("PT_RUN_MAC_INTEGRATION") == "1", "requires explicit macOS installer fixture admission")
    def test_installer_rerun_keeps_runner_and_daily_without_live_effects(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            base = Path(d)
            toolkit = base / "toolkit"
            shutil.copytree(
                ROOT, toolkit, ignore=shutil.ignore_patterns("var", "__pycache__")
            )
            # Even if dry-run handling regresses, normal bootstrap cannot reach
            # pm or brew on the live machine. Chmod can touch only the copied tree.
            sentinel = "#!/bin/zsh\nexit 97\n"
            (toolkit / "bin/pm").write_text(sentinel)
            tools = base / "tools"
            tools.mkdir()
            (base / "zdot").mkdir()
            (tools / "brew").write_text(sentinel)
            (tools / "brew").chmod(0o700)
            out = base / "plists"
            out.mkdir()
            env = {
                **os.environ,
                "ZDOTDIR": str(base / "zdot"),
                "PATH": str(tools) + ":/usr/bin:/bin:/usr/sbin:/sbin",
                "STORAGE_PYTHON": sys.executable,
            }
            for _ in range(2):
                p = subprocess.run(
                    [
                        "/bin/zsh",
                        str(toolkit / "install.sh"),
                        "--dry-run",
                        "--output",
                        str(out),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(p.returncode, 0, p.stderr)
                files = sorted(out.glob("*.plist"))
                self.assertEqual(len(files), 2)
                mem = plistlib.loads(
                    (out / "local.powertoolkit.mem-watchdog.plist").read_bytes()
                )
                daily = plistlib.loads(
                    (out / "local.powertoolkit.storage-daily.plist").read_bytes()
                )
                self.assertIn("monitor", mem["ProgramArguments"])
                self.assertTrue(mem["KeepAlive"])
                self.assertNotIn("StartInterval", mem)
                self.assertEqual(
                    daily["StartCalendarInterval"], {"Hour": 4, "Minute": 15}
                )
                self.assertFalse(daily["RunAtLoad"])
            unknown = subprocess.run(
                ["/bin/zsh", str(toolkit / "install.sh"), "--typo"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(unknown.returncode, 2)

    def test_private_size_not_blocks_and_incomplete_is_not_zero(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            (Path(d) / "file").write_bytes(b"x" * 17)
            r = s.allocation(Path(d), timeout=10, private_reader=lambda _: 7)
            self.assertEqual(r["bytes"], 7)
            r = s.allocation(Path(d), timeout=10, private_reader=lambda _: None)
            self.assertIsNone(r["bytes"])
            self.assertEqual(r["status"], "unavailable-or-partial")

    def test_fast_fall_incident_attributes_writer_and_rejects_pid_reuse(self):
        before = [{"pid": 7, "start": 11, "written": 10, "name": "worker"}]
        after = [{"pid": 7, "start": 11, "written": 250, "name": "worker"}]
        proc = {
            7: {"pid": 7, "ppid": 9, "start": 11, "name": "worker"},
            9: {"pid": 9, "ppid": 1, "start": 5, "name": "codex"},
        }
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            with patch.object(
                s,
                "native_metrics",
                return_value={"swap_used_bytes": 5, "vm_used_bytes": 9},
            ):
                runner = s.Recorder(
                    Path(d),
                    low=10,
                    ancestor_reader=lambda pid: proc.get(pid),
                    cwd_reader=lambda pid: str(Path.home() / "Development/lane"),
                )
                runner.record(0, 5_000_000_000, before, {"readable": 1})
                runner.record(5, 2_900_000_000, after, {"readable": 1})
            files = list(Path(d).glob("incident-*.json"))
            self.assertEqual(len(files), 1)
            incident = json.loads(files[0].read_text())
            w = incident["candidate_writers"][0]
            self.assertEqual(w["delta_bytes"], 240)
            self.assertEqual(w["ancestor"]["pid"], 9)
            self.assertEqual(w["ancestor"]["start"], 5)
            self.assertEqual(w["ancestor"]["cwd"], "~/Development/lane")
            self.assertEqual(incident["reasons"], ["fast-fall"])
            proc[7]["start"] = 99
            self.assertEqual(
                s.attribute_writer(w, lambda pid: proc.get(pid), lambda pid: "/tmp")[
                    "status"
                ],
                "unavailable",
            )

    def test_daily_latest_has_growth_swap_vm_and_candidates(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            prior = {
                "roots": {"A": {"bytes": 2}},
                "native": {"swap_used_bytes": 4, "vm_used_bytes": 6},
            }
            (Path(d) / "2000-01-01.json").write_text(json.dumps(prior))
            with (
                patch.object(
                    s, "allocation", return_value={"bytes": 12, "status": "ok"}
                ),
                patch.object(s, "free_bytes", return_value=500),
                patch.object(
                    s,
                    "native_metrics",
                    return_value={"swap_used_bytes": 9, "vm_used_bytes": 15},
                ),
            ):
                r = s.daily(Path(d), {"A": "/fabricated"}, incidents=[])
            md = (Path(d) / "latest.md").read_text()
            self.assertIn("A", md)
            self.assertEqual(r["ranked_changes"][0]["delta_bytes"], 10)
            self.assertEqual(r["trends"], {"swap_used_bytes": 5, "vm_used_bytes": 9})
            self.assertIn("Candidate writers", md)
            self.assertIn("Swap", md)
            self.assertIn("VM", md)


class MoreContracts(unittest.TestCase):
    def test_discovery_covers_temp_chrome_worktrees_and_lane_growth_roots(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            base = Path(d)
            for rel in [
                "temp/T",
                "temp/C",
                "temp/X/com.google.Chrome.code_sign_clone/code_sign_clone.one",
                "dev/project-wt-one",
                "dev/personal-work-ecosystem/local/day/LANE",
            ]:
                (base / rel).mkdir(parents=True)
            cfg = {
                "STORAGE_DEVELOPMENT": str(base / "dev"),
                "STORAGE_PWE_LOCAL": str(base / "dev/personal-work-ecosystem/local"),
                "STORAGE_USER_TEMP": str(base / "temp/T"),
                "STORAGE_USER_CACHE": "",
                "STORAGE_SCAN_TIMEOUT_SEC": "10",
            }
            real_glob = Path.glob
            def local_glob(path, pattern):
                if str(path) == "/private/tmp":
                    return iter(())
                return real_glob(path, pattern)
            with patch.object(Path, "glob", local_glob):
                roots, missing = s.discover_roots(cfg)
            self.assertIn("user-temp-C", roots)
            self.assertIn("user-temp-X", roots)
            self.assertEqual(
                roots["lane:day/LANE"],
                str(base / "dev/personal-work-ecosystem/local/day/LANE"),
            )
            self.assertEqual(
                roots["worktree:project-wt-one"], str(base / "dev/project-wt-one")
            )
            self.assertEqual(s.chrome_clone_count(roots["user-temp-X"]), 1)

    def test_ancestry_unavailable_and_sensitive_cwd_not_exported(self):
        writer = {"pid": 4, "start": 3}
        self.assertEqual(
            s.attribute_writer(writer, lambda _: None)["status"], "unavailable"
        )
        self.assertEqual(
            s.mask_cwd(str(Path.home() / "private-writing/diary")), "unavailable"
        )

    @unittest.skipUnless(Path("/bin/zsh").exists(), "requires zsh config evaluation")
    def test_stock_rollback_is_periodic_and_not_a_runner(self):
        cfg = s.settings(ROOT)
        cfg["STORAGE_ENABLE"] = "0"
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            s.generate_plists(d, ROOT, cfg)
            paths = list(Path(d).glob("*.plist"))
            self.assertEqual(len(paths), 2)
            mem = plistlib.loads((Path(d) / "local.powertoolkit.mem-watchdog.plist").read_bytes())
            self.assertEqual(mem["StartInterval"], 60)
            self.assertEqual(
                mem["ProgramArguments"], [str(ROOT / "libexec/mem-watchdog.sh")]
            )

    def test_symlinks_hardlinks_partial_and_timeout(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            p = Path(d)
            (p / "a").write_bytes(b"abc")
            os.link(p / "a", p / "b")
            (p / "link").symlink_to(p / "a")
            reading = s.allocation(p, private_reader=lambda _: 5)
            self.assertEqual(reading["bytes"], 5)
            self.assertEqual(reading["files"], 1)
            self.assertIsNone(s.allocation(p, timeout=0)["bytes"])

    @unittest.skipUnless(sys.platform == "darwin" and os.environ.get("PT_RUN_MAC_INTEGRATION") == "1", "requires explicit macOS pm fixture admission")
    def test_real_apply_rerun_in_stubbed_toolkit_keeps_runner(self):
        # Execute actual cmd_apply, not its source text. Every external mutation is
        # replaced by a fail-closed fixture and all writable paths are lane-local.
        import shutil

        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            p = Path(d)
            toolkit = p / "toolkit"
            shutil.copytree(
                ROOT, toolkit, ignore=shutil.ignore_patterns("var", "__pycache__")
            )
            agents = p / "agents"
            tools = p / "tools"
            tools.mkdir()
            (p / "zdot").mkdir()
            for name in ["launchctl", "pmset", "sudo", "blueutil", "sleepwatcher"]:
                body = "exit 0\n"
                if name == "launchctl":
                    body = 'if [[ "$1" == print && "$2" == */*/* ]]; then print -u2 "Could not find service"; exit 1; fi\nexit 0\n'
                if name == "sudo":
                    body = "exit 1\n"
                if name == "pmset":
                    body = '[[ "$1" == -g ]] || exit 90\nexit 0\n'
                f = tools / name
                f.write_text("#!/bin/zsh\n" + body)
                f.chmod(0o700)
            env = {
                **os.environ,
                "ZDOTDIR": str(p / "zdot"),
                "PATH": str(tools) + ":/usr/bin:/bin:/usr/sbin:/sbin",
                "PT_AGENTS_DIR": str(agents),
                "STORAGE_PYTHON": sys.executable,
            }
            for _ in range(2):
                r = subprocess.run(
                    ["/bin/zsh", str(toolkit / "bin/pm"), "apply"],
                    env=env,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                )
                self.assertEqual(r.returncode, 0, r.stderr)
                mem = plistlib.loads(
                    (agents / "local.powertoolkit.mem-watchdog.plist").read_bytes()
                )
                self.assertIn("monitor", mem["ProgramArguments"])
                self.assertTrue(
                    (agents / "local.powertoolkit.storage-daily.plist").exists()
                )


class FailureSignals(unittest.TestCase):
    @unittest.skipUnless(Path("/bin/zsh").exists(), "requires zsh config evaluation")
    def test_bad_config_persists_failure_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            p = Path(d)
            toolkit = p / "broken"
            toolkit.mkdir()
            (toolkit / "config.sh").write_text("exit 2\n")
            r = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "libexec/storage_tracking.py"),
                    "monitor",
                    "--toolkit",
                    str(toolkit),
                    "--output",
                    str(p / "state"),
                    "--once",
                    "--skip-memory-watchdog",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(r.returncode, 0)
            failure = json.loads((p / "state/last-error.json").read_text())
            self.assertEqual(failure["status"], "down")
            self.assertEqual(failure["phase"], "monitor")


class NativeMetricContracts(unittest.TestCase):
    def test_role_sizes_swap_units_and_private_fields_not_exported(self):
        payload = plistlib.dumps(
            {
                "Containers": [
                    {
                        "CapacityCeiling": 100,
                        "CapacityFree": 20,
                        "ContainerUUID": "must-not-export",
                        "Volumes": [
                            {"Roles": ["VM"], "CapacityInUse": 11},
                            {
                                "Roles": ["Data"],
                                "CapacityInUse": 60,
                                "Name": "must-not-export",
                            },
                        ],
                    }
                ]
            }
        )

        def native(args, **kwargs):
            return subprocess.CompletedProcess(
                args,
                0,
                payload if "list" in args else plistlib.dumps({"Snapshots": []}),
            )

        def run(args, **kwargs):
            if args[0] == "/usr/sbin/sysctl":
                return 0, "total = 20G used = 1.5G free = 18.5G", 0
            return 0, "{}", 0

        with (
            patch.object(s.subprocess, "run", side_effect=native),
            patch.object(s, "run", side_effect=run),
        ):
            report = s.native_metrics()
        self.assertEqual(report["swap_used_bytes"], 1610612736)
        self.assertEqual(report["vm_used_bytes"], 11)
        self.assertNotIn("must-not-export", json.dumps(report))

    def test_unknown_native_metrics_are_not_zero(self):
        with (
            patch.object(s.subprocess, "run", side_effect=OSError),
            patch.object(s, "run", return_value=(-1, "", 1)),
        ):
            r = s.native_metrics()
        self.assertIsNone(r["swap_used_bytes"])
        self.assertIsNone(r["vm_used_bytes"])


if __name__ == "__main__":
    unittest.main()
