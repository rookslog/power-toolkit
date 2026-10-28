"""G1: optional storage startup must not gate the stock memory guardian."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libexec"))
import storage_tracking as s
from test_storage_repairs import config, Child, TEMP


class StartupIsolation(unittest.TestCase):
    def check_invalid_setting(self, key, value, entry, implicit_output=False):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            state = Path(d) / "state"
            cfg = {
                **config(), "STORAGE_STATE": str(state),
                "STORAGE_SCAN_TIMEOUT_SEC": "180",
                "STORAGE_ATTRIBUTION_TIMEOUT_SEC": "8", key: value,
            }
            # Keep the real settings validator; replace only external zsh reads.
            def shell_read(args, timeout=15):
                if "MEM_DISK_LOW_BYTES" in args[3]:
                    return 0, "10 60\n", 0
                return 0, "".join(f"{k}\0{v}\0" for k, v in cfg.items()), 0

            dispatches = []
            def spawn(args, **kwargs):
                dispatches.append((args, kwargs))
                return Child()

            clock = [0]
            class Stopped(BaseException):
                pass
            def sleep(seconds):
                clock[0] += seconds
                if clock[0] >= 121:
                    raise Stopped()

            prior_umask = os.umask(0o077)
            self.addCleanup(os.umask, prior_umask)
            stderr = io.StringIO()
            argv = ["storage_tracking.py", "monitor", "--toolkit", str(ROOT)]
            if not implicit_output:
                argv += ["--output", str(state)]
            # Join each real worker before advancing virtual time: deterministic
            # retries without depending on OS thread scheduling.
            import threading
            real_thread = threading.Thread
            class JoinedThread(real_thread):
                def start(self):
                    super().start()
                    self.join()

            with (
                patch.object(s, "run", side_effect=shell_read),
                patch.object(s.subprocess, "Popen", side_effect=spawn),
                patch.object(s.time, "monotonic", side_effect=lambda: clock[0]),
                patch.object(s.time, "sleep", side_effect=sleep),
                patch.object(s.threading, "Thread", JoinedThread),
                patch.object(sys, "argv", argv),
                contextlib.redirect_stderr(stderr),
            ):
                try:
                    if entry == "main":
                        s.main()
                    else:
                        s.monitor(state, ROOT)
                except Stopped:
                    pass
                except (RuntimeError, ValueError) as error:
                    self.fail(f"storage validation escaped before guardian: {type(error).__name__}")

            self.assertEqual(len(dispatches), 3)
            for args, kwargs in dispatches:
                self.assertEqual(args, ["/bin/zsh", "-f", str(ROOT / "libexec/mem-watchdog.sh")])
                self.assertIsNone(kwargs.get("stderr"), "guardian stderr must reach runner log")
            failures = [json.loads(line) for line in stderr.getvalue().splitlines()]
            self.assertGreaterEqual(len(failures), 2)
            self.assertTrue(all(f["status"] == "telemetry-unavailable" for f in failures))
            if not implicit_output:
                self.assertEqual(json.loads((state / "last-error.json").read_text())["status"], "telemetry-unavailable")

    def test_invalid_storage_settings_do_not_gate_monitor_or_main(self):
        for key, value in [("STORAGE_INTERVAL_SEC", "0"), ("STORAGE_WINDOW_SEC", ""), ("STORAGE_INCIDENT_SLOTS", "forty")]:
            for entry in ("monitor", "main"):
                with self.subTest(key=key, entry=entry):
                    self.check_invalid_setting(key, value, entry)

    def test_invalid_storage_settings_do_not_gate_default_output_startup(self):
        for key, value in [("STORAGE_INTERVAL_SEC", "0"), ("STORAGE_WINDOW_SEC", ""), ("STORAGE_INCIDENT_SLOTS", "forty")]:
            with self.subTest(key=key):
                self.check_invalid_setting(key, value, "main", implicit_output=True)

    def test_each_generated_job_keeps_stderr_in_configured_log_directory(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            output = Path(d) / "agents"
            cfg = {**config(), "PT_LOG": str(Path(d) / "custom logs")}
            s.generate_plists(output, ROOT, cfg)
            for path in output.glob("*.plist"):
                job = plistlib.loads(path.read_bytes())
                self.assertEqual(job["StandardErrorPath"], str(Path(cfg["PT_LOG"]) / (job["Label"] + ".err.log")))

    def test_valid_monitor_resolves_default_output_in_telemetry(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            state = Path(d) / "state"
            cfg = {**config(), "STORAGE_STATE": str(state)}
            prior_umask = os.umask(0o077)
            self.addCleanup(os.umask, prior_umask)
            with (
                patch.object(sys, "argv", ["storage_tracking.py", "monitor", "--once"]),
                patch.object(s, "config_values", return_value={"low": 10, "memory_interval": 60}),
                patch.object(s, "settings", return_value=cfg),
                patch.object(s, "free_bytes", return_value=100),
                patch.object(s, "processes", return_value=([{"pid": 7, "start": 20, "written": 1, "name": "worker"}], {"readable": 1, "unavailable": 0})),
                patch.object(s.subprocess, "Popen", return_value=Child()),
            ):
                s.main()
            status = json.loads((state / "monitor/status.json").read_text())
            self.assertEqual(status["status"], "observing")
            self.assertEqual(status["free_bytes"], 100)
            self.assertIsNone(status["guardian"]["dispatch_error"])


class MissingInterpreter(unittest.TestCase):
    def run_fallback(self, shell, adapted=False):
        for interval in (30, 60, 120):
            with self.subTest(interval=interval), tempfile.TemporaryDirectory(dir=TEMP, prefix="toolkit with spaces ") as d:
                toolkit = Path(d)
                (toolkit / "bin").mkdir()
                (toolkit / "libexec").mkdir()
                (toolkit / "stubs").mkdir()
                wrapper = (ROOT / "bin/storage-tracking").read_text()
                if adapted:
                    # Only adapt zsh bootstrap syntax. The fallback branch, its
                    # executable check, guardian call, and sleep run unchanged.
                    wrapper = wrapper.replace("emulate -L zsh", ":")
                    wrapper = wrapper.replace("${0:A:h:h}", '"$TEST_TOOLKIT"')
                (toolkit / "bin/storage-tracking").write_text(wrapper)
                (toolkit / "config.sh").write_text(
                    'STORAGE_PYTHON="$PT_ROOT/missing-python"\n'
                    f'MEM_WATCHDOG_INTERVAL_SEC={interval}\n'
                    'PATH="$PT_ROOT/stubs:$PATH"\n'
                )
                (toolkit / "libexec/mem-watchdog.sh").write_text(
                    'printf "guardian\\n" >> "$TRACE"\n'
                    'printf "guardian diagnostic\\n" >&2\nexit 7\n'
                )
                sleep = toolkit / "stubs/sleep"
                sleep.write_text('#!/bin/bash\nprintf "sleep %s\\n" "$1" >> "$TRACE"\n'
                                 '[[ $(wc -l < "$TRACE") -lt 6 ]] || exit 23\n')
                sleep.chmod(0o700)
                trace = toolkit / "trace"
                env = {**os.environ, "TEST_TOOLKIT": str(toolkit), "TRACE": str(trace)}
                if adapted:
                    # Linux has no /bin/zsh: substitute only the interpreter
                    # boundary for the fabricated guardian, never real services.
                    command = [shell, "-c", '/bin/zsh() { /bin/bash "$@"; }; source "$TEST_TOOLKIT/bin/storage-tracking" monitor']
                else:
                    command = [shell, "-f", str(toolkit / "bin/storage-tracking"), "monitor"]
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
                self.assertTrue(trace.exists(), f"guardian never started: {result.stderr}")
                self.assertEqual(trace.read_text().splitlines(), ["guardian", f"sleep {interval}"] * 3)
                self.assertEqual(result.returncode, 23)
                self.assertEqual(result.stderr.splitlines(), ["guardian diagnostic"] * 3)

    def test_missing_interpreter_fallback_shared_shell_logic(self):
        self.run_fallback("/bin/bash", adapted=True)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "requires native zsh wrapper execution")
    def test_missing_interpreter_runs_guardian_at_stock_cadence(self):
        self.run_fallback("/bin/zsh")
