"""Review counterexamples: telemetry isolation, rollback, ancestry and job pairs."""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'libexec'))
import storage_tracking as s
import storage_native as n

TEMP = ROOT.parent / 'test-tmp'
TEMP.mkdir(exist_ok=True)


def config(enabled='1'):
    return {
        'STORAGE_ENABLE': enabled, 'STORAGE_INTERVAL_SEC': '1',
        'STORAGE_WINDOW_SEC': '600', 'STORAGE_INCIDENT_SLOTS': '40',
        'STORAGE_CAPTURE_COOLDOWN_SEC': '300',
        'STORAGE_DAILY_HOUR': '4', 'STORAGE_DAILY_MINUTE': '15',
        'MEM_DATA_VOLUME': '/fabricated',
        'MEM_WATCHDOG_LABEL': 'local.powertoolkit.mem-watchdog',
        'STORAGE_DAILY_LABEL': 'local.powertoolkit.storage-daily',
    }


class Child:
    def poll(self):
        return 0


class GuardianRepairs(unittest.TestCase):
    def run_monitor(self, directory, fail_write=False, block_metrics=False):
        stop = threading.Event()
        release = threading.Event()
        entered = threading.Event()
        dispatched = threading.Event()
        dispatches = []
        errors = []
        workers = []
        real_thread = threading.Thread
        failed_writes = []
        actual_atomic = s.atomic

        def atomic(path, value):
            if fail_write:
                failed_writes.append(Path(path).name)
                raise OSError(errno.ENOSPC, 'fabricated full state filesystem')
            return actual_atomic(path, value)

        def metrics(*args):
            entered.set()
            if block_metrics:
                release.wait(2)
            return {'swap_used_bytes': None, 'vm_used_bytes': None}

        def child(*args, **kwargs):
            dispatches.append(s.time.monotonic())
            if len(dispatches) >= 3:
                dispatched.set()
                stop.set()
            return Child()

        class Stopped(Exception):
            pass

        real_sleep = s.time.sleep

        def sleep(seconds):
            if stop.is_set():
                raise Stopped()
            real_sleep(min(seconds, .01))

        def run():
            try:
                s.monitor(directory, ROOT)
            except Stopped:
                pass
            except Exception as error:
                errors.append(error)

        with (
            patch.object(s, 'config_values', return_value={'low': 10, 'memory_interval': .55 if fail_write else .04}),
            patch.object(s, 'settings', return_value=config()),
            patch.object(s, 'free_bytes', return_value=5),
            patch.object(s, 'processes', return_value=([{'pid': 7, 'start': 20, 'written': 1, 'name': 'worker'}], {'readable': 1, 'unavailable': 0})),
            patch.object(s, 'native_metrics', side_effect=metrics),
            patch.object(s, 'atomic', side_effect=atomic),
            patch.object(s.time, 'sleep', side_effect=sleep),
            patch.object(s.subprocess, 'Popen', side_effect=child),
            patch.object(threading, 'Thread', side_effect=lambda **kw: workers.append(real_thread(**kw)) or workers[-1]),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            runner = real_thread(target=run)
            runner.start()
            try:
                self.assertTrue(dispatched.wait(1.6), f'guardian stopped: {errors}, dispatches={len(dispatches)}')
                if block_metrics:
                    self.assertTrue(entered.is_set())
                    self.assertFalse(release.is_set())
            finally:
                release.set()
                stop.set()
                runner.join(2)
                for worker in workers:
                    worker.join(2)
            self.assertFalse(runner.is_alive())
            self.assertEqual(errors, [])
            self.assertGreaterEqual(len(dispatches), 3)
            if fail_write:
                self.assertGreaterEqual(failed_writes.count("ring.json"), 2)
            if block_metrics:
                self.assertEqual(len(workers), 1)

    def test_B1_repeated_write_failures_do_not_stop_guardian(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            self.run_monitor(Path(d), fail_write=True)

    def test_B1_blocked_native_metrics_do_not_delay_later_guardians(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            self.run_monitor(Path(d), block_metrics=True)

    def test_B1_unwritable_state_initialization_does_not_gate_guardian(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            path = Path(d) / 'not-a-directory'
            path.write_text('fixture')
            self.run_monitor(path)


class DailyRepairs(unittest.TestCase):
    def test_B2_disabled_daily_has_no_discovery_or_writes(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            output = Path(d) / 'absent'
            cfg = {**config('0'), 'STORAGE_STATE': str(output)}
            def forbidden(*args, **kwargs):
                self.fail('disabled daily performed discovery, metrics or output')
            prior_umask = os.umask(0o077)
            self.addCleanup(os.umask, prior_umask)
            with (
                patch.object(sys, 'argv', ['storage_tracking.py', 'daily', '--output', str(output)]),
                patch.object(s, 'settings', return_value=cfg),
                patch.object(s, 'discover_roots', side_effect=forbidden),
                patch.object(s, 'native_metrics', side_effect=forbidden),
                patch.object(s, 'atomic', side_effect=forbidden),
            ):
                s.main()
            self.assertFalse(output.exists())

    def test_B2_enabled_disabled_enabled_replaces_schedule(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            path = Path(d) / 'local.powertoolkit.storage-daily.plist'
            s.generate_plists(d, ROOT, config())
            with patch.object(s, 'config_values', return_value={'memory_interval': 60}):
                s.generate_plists(d, ROOT, config('0'))
            disabled = plistlib.loads(path.read_bytes())
            self.assertTrue(disabled.get('Disabled'))
            self.assertNotIn('StartCalendarInterval', disabled)
            self.assertFalse(disabled['RunAtLoad'])
            s.generate_plists(d, ROOT, config())
            enabled = plistlib.loads(path.read_bytes())
            self.assertFalse(enabled.get('Disabled', False))
            self.assertEqual(enabled['StartCalendarInterval'], {'Hour': 4, 'Minute': 15})

    def test_B2_shell_rollback_plist_is_disabled_without_python(self):
        import subprocess
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            output = Path(d) / 'daily.plist'
            env = {**os.environ, 'LIBRARY': str(ROOT / 'libexec/storage-jobs.sh'), 'OUT': str(output)}
            p = subprocess.run(['/bin/bash', '-c', 'source "$LIBRARY"; STORAGE_DAILY_LABEL=local.powertoolkit.storage-daily; storage_disabled_daily "$OUT"'], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            disabled = plistlib.loads(output.read_bytes())
            self.assertEqual(disabled['ProgramArguments'], ['/usr/bin/true'])
            self.assertTrue(disabled['Disabled'])
            self.assertFalse(disabled['RunAtLoad'])
            self.assertNotIn('StartCalendarInterval', disabled)


class AncestryRepairs(unittest.TestCase):
    def ancestry(self, rows, read=None, cwd=None):
        return n.attribute_writer({'pid': 7, 'start': 20}, read or rows.get, cwd or (lambda _: '/tmp/lane'))

    def test_B3_recycled_younger_parent_is_unavailable(self):
        rows = {7: {'pid': 7, 'ppid': 9, 'start': 20, 'name': 'worker'},
                9: {'pid': 9, 'ppid': 1, 'start': 30, 'name': 'codex'}}
        self.assertEqual(self.ancestry(rows)['status'], 'unavailable')

    def test_B3_changed_direct_or_intermediate_edge_is_unavailable(self):
        for changed in (7, 8):
            with self.subTest(changed=changed):
                rows = {7: {'pid': 7, 'ppid': 8, 'start': 20, 'name': 'worker'},
                        8: {'pid': 8, 'ppid': 9, 'start': 15, 'name': 'helper'},
                        9: {'pid': 9, 'ppid': 1, 'start': 10, 'name': 'codex'}}
                def cwd(_):
                    rows[changed] = {**rows[changed], 'ppid': 1}
                    return '/tmp/lane'
                self.assertEqual(self.ancestry(rows, cwd=cwd)['status'], 'unavailable')

    def test_B3_intermediate_identity_reuse_is_unavailable(self):
        rows = {7: {'pid': 7, 'ppid': 8, 'start': 20, 'name': 'worker'},
                8: {'pid': 8, 'ppid': 9, 'start': 15, 'name': 'helper'},
                9: {'pid': 9, 'ppid': 1, 'start': 10, 'name': 'codex'}}
        def cwd(_):
            rows[8] = {**rows[8], 'start': 16}
            return '/tmp/lane'
        self.assertEqual(self.ancestry(rows, cwd=cwd)['status'], 'unavailable')

    def test_B3_stable_chain_and_direct_writer_controls(self):
        rows = {7: {'pid': 7, 'ppid': 8, 'start': 20, 'name': 'worker'},
                8: {'pid': 8, 'ppid': 9, 'start': 15, 'name': 'helper'},
                9: {'pid': 9, 'ppid': 1, 'start': 10, 'name': 'codex'}}
        self.assertEqual(self.ancestry(rows)['pid'], 9)
        rows[7] = {**rows[7], 'start': 99}
        self.assertEqual(self.ancestry(rows)['status'], 'unavailable')


class GenerationRepairs(unittest.TestCase):
    def test_B4_failed_first_or_second_stage_leaves_prior_bytes_and_modes(self):
        for failed in ('mem-watchdog', 'storage-daily'):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory(dir=TEMP) as d:
                p = Path(d)
                for label in ('mem-watchdog', 'storage-daily'):
                    target = p / f'local.powertoolkit.{label}.plist'
                    target.write_bytes(b'prior bytes ' + label.encode())
                    target.chmod(0o640)
                before = {f.name: (f.read_bytes(), f.stat().st_mode & 0o777) for f in p.iterdir()}
                real_atomic = s.atomic_text
                def fail(path, value):
                    if Path(path).name.endswith(failed + '.plist'):
                        raise OSError(errno.ENOSPC, 'fabricated stage failure')
                    return real_atomic(path, value)
                with patch.object(s, 'atomic_text', side_effect=fail):
                    with self.assertRaises(OSError):
                        s.generate_plists(p, ROOT, config())
                after = {f.name: (f.read_bytes(), f.stat().st_mode & 0o777) for f in p.glob('*.plist')}
                self.assertEqual(after, before)
    def test_B4_each_generation_publication_failure_restores_prior_files(self):
        for failed in ('mem-watchdog', 'storage-daily'):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory(dir=TEMP) as d:
                root = Path(d)
                before = {}
                for name in ('mem-watchdog', 'storage-daily'):
                    target = root / ('local.powertoolkit.' + name + '.plist')
                    target.write_bytes(('old-' + name).encode())
                    target.chmod(0o640)
                    before[target.name] = (target.read_bytes(), 0o640)
                replace = s.os.replace
                triggered = False
                def fail(source, target):
                    nonlocal triggered
                    if Path(target).parent == root and Path(target).name.endswith(failed + '.plist') and not triggered:
                        triggered = True
                        raise OSError(errno.ENOSPC, 'fabricated publication failure')
                    return replace(source, target)
                with patch.object(s.os, 'replace', side_effect=fail):
                    with self.assertRaises(OSError):
                        s.generate_plists(root, ROOT, config())
                after = {f.name: (f.read_bytes(), f.stat().st_mode & 0o777) for f in root.glob('*.plist')}
                self.assertEqual(after, before)

    def test_B4_failed_generation_restore_retains_recovery_bytes(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            root = Path(d)
            memory = root / 'local.powertoolkit.mem-watchdog.plist'
            memory.write_bytes(b'old-memory')
            memory.chmod(0o640)
            replace = s.os.replace
            def fail(source, target):
                if Path(target).parent == root and (Path(target).name.endswith('storage-daily.plist') or str(source).endswith('.prior')):
                    raise OSError(errno.ENOSPC, 'fabricated publication and restore failure')
                return replace(source, target)
            with patch.object(s.os, 'replace', side_effect=fail):
                with self.assertRaises(OSError):
                    s.generate_plists(root, ROOT, config())
            copies = list(root.glob('.storage-generate-*/*.prior'))
            self.assertEqual([p.read_bytes() for p in copies], [b'old-memory'])
            self.assertEqual(copies[0].stat().st_mode & 0o777, 0o640)


class ApplyRepairs(unittest.TestCase):
    def transaction(self, fail='', enabled=True, loaded=True, prior=True, rollback_failure=False):
        """Run source-only transaction library with local file/service substitutes.

        No pm, installer, launchctl, pmset or native sampler is invoked.
        Bash exercises the library's shared shell subset; zsh remains a Mac gate.
        """
        import subprocess
        with tempfile.TemporaryDirectory(dir=TEMP) as d:
            root = Path(d)
            agents = root / 'agents'
            agents.mkdir()
            for name in ('memory', 'daily'):
                if prior:
                    p = agents / (name + '.plist')
                    p.write_bytes(('prior-' + name).encode())
                    p.chmod(0o640)
            script = r'''
source "$LIBRARY"
MEM_WATCHDOG_LABEL=memory
STORAGE_DAILY_LABEL=daily
STORAGE_ENABLE=$ENABLED
AGENTS_DIR=$AGENTS
storage_prepare() {
  local name
  for name in memory daily; do
    [[ "$FAIL" == "prepare-$name" ]] && return 1
    printf 'new-%s' "$name" > "$1/$name.plist" || return 1
    chmod 600 "$1/$name.plist" || return 1
  done
}
storage_validate() { [[ "$FAIL" != validation ]]; }
agent_loaded() {
  [[ "$FAIL" == "state-$1" ]] && return 2
  [[ "$LOADED" == 1 ]]
}
reload_agent() {
  printf 'reload %s %s\n' "$1" "$(cat "$2")" >> "$RECEIPT"
  if [[ "$FAIL" == "reload-$1" && ! -e "$ONCE" ]]; then
    : > "$ONCE"; return 1
  fi
}
stop_agent() {
  printf 'stop %s\n' "$1" >> "$RECEIPT"
  if [[ "$FAIL" == "stop-$1" && ! -e "$ONCE" ]]; then
    : > "$ONCE"; return 1
  fi
}
mv() {
  if [[ "$ROLLBACK_FAILURE" == 1 && "$2" == */prior/memory.plist ]]; then
    return 1
  fi
  if [[ "$FAIL" == "publish-$(basename "${@: -1}" .plist)" && ! -e "$ONCE" ]]; then
    : > "$ONCE"; return 1
  fi
  command mv "$@"
}
storage_apply
'''
            env = {**os.environ, 'LIBRARY': str(ROOT / 'libexec/storage-jobs.sh'),
                   'AGENTS': str(agents), 'FAIL': fail, 'ENABLED': str(int(enabled)),
                   'LOADED': str(int(loaded)), 'RECEIPT': str(root / 'events'),
                   'ONCE': str(root / 'injected'), 'ROLLBACK_FAILURE': str(int(rollback_failure))}
            p = subprocess.run(['/bin/bash', '-c', script], env=env, capture_output=True, text=True)
            files = {f.name: (f.read_bytes(), f.stat().st_mode & 0o777) for f in agents.glob('*.plist')}
            events = (root / 'events').read_text() if (root / 'events').exists() else ''
            if fail:
                self.assertNotEqual(p.returncode, 0)
                expected = {name + '.plist': (('prior-' + name).encode(), 0o640) for name in ('memory', 'daily')} if prior else {}
                if rollback_failure:
                    expected['memory.plist'] = (b'new-memory', 0o600)
                    self.assertIn('storage file rollback incomplete', p.stderr)
                    self.assertIn('service restore withheld', p.stderr)
                    backup = next(agents.glob('.storage-apply.*/prior/memory.plist'))
                    self.assertEqual(backup.read_bytes(), b'prior-memory')
                    self.assertEqual(backup.stat().st_mode & 0o777, 0o640)
                    self.assertNotIn('prior-memory', events)
                    self.assertEqual(files, expected)
                    return
                self.assertEqual(files, expected, p.stderr)
                if fail.startswith(('reload-', 'stop-')):
                    if loaded:
                        self.assertIn('reload memory prior-memory', events)
                        self.assertIn('reload daily prior-daily', events)
                    else:
                        self.assertIn('stop memory', events)
                        self.assertIn('stop daily', events)
                else:
                    self.assertEqual(events, '')
            else:
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(files, {name + '.plist': (('new-' + name).encode(), 0o600) for name in ('memory', 'daily')})
                self.assertIn('reload memory new-memory', events)
                self.assertIn('reload daily new-daily' if enabled else 'stop daily', events)

    def test_B4_stage_validation_and_each_publication_failure_restore_files(self):
        for failed in ('prepare-memory', 'prepare-daily', 'validation', 'state-memory', 'state-daily', 'publish-memory', 'publish-daily'):
            with self.subTest(failed=failed):
                self.transaction(failed)

    def test_B4_each_reload_and_disabled_stop_failure_restore_loaded_jobs(self):
        for failed, enabled in (('reload-memory', True), ('reload-daily', True), ('stop-daily', False)):
            with self.subTest(failed=failed):
                self.transaction(failed, enabled=enabled)

    def test_B4_failure_restores_absent_and_unloaded_jobs(self):
        self.transaction('reload-daily', prior=False, loaded=False)

    def test_B4_failed_restore_is_visible_and_retains_recovery(self):
        self.transaction('reload-memory', rollback_failure=True)

    def test_B4_success_enable_and_python_free_disabled_reconciliation(self):
        self.transaction()
        self.transaction(enabled=False)


if __name__ == '__main__':
    unittest.main()
