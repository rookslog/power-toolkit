# Storage tracking — metadata only

Power Toolkit owns both `local.powertoolkit.mem-watchdog` (replacement runner,
KeepAlive, 10-second restart throttle) and `local.powertoolkit.storage-daily`
(04:15 local, no RunAtLoad). `install.sh` delegates normal installation to
`pm install` → `pm apply`; apply uses the same generator as isolated dry-run.
There is one memory scheduler, not an additional standalone guardian.

## Configuration and data

`config.sh` is the source of paths, thresholds, schedules and Python selection.
Set `STORAGE_PYTHON` to a Python 3.9+ executable (Apollo default Homebrew path).
No pip packages. Launchd calls `bin/storage-tracking`, which resolves config and
executes `libexec/storage_tracking.py` with bytecode generation disabled.

- Five-second samples: O(1) Data free space and per-PID native disk-I/O counters.
  Identity is `(PID, ri_proc_start_abstime)`, measured in mach absolute ticks.
- Capture below configured `MEM_DISK_LOW_BYTES`, or a drop **greater than**
  2 decimal GB from a peak within 600 seconds. Existing low/swap/pressure
  thresholds and mode-dependent memory intervals are unchanged.
- Incident candidates aggregate the retained 25 interval summaries (top 20
  readable writers per sample). At incident time, walk at most 32 parents to
  the nearest `claude`, `codex`, `node` or `.app` executable. Record PID, name,
  start identity, wall start time if readable, and home-masked cwd. A global
  eight-second attribution budget is best-effort; individual OS calls can
  overrun it. Every traversed PID/start/PPID relationship is re-read after cwd;
  younger parents and changed edges become unavailable. These reads are not an
  atomic kernel ancestry snapshot. Inaccessible/exited/reused PIDs are unavailable.
- Cwd is requested only for an eligible agent/app ancestor with `lsof -d cwd`;
  no full argv, environment or file data is read or retained. Sensitive-path
  components are suppressed. Node ancestry is a candidate, not proof that
  node is an agent or that it owns the storage increase.
- Serial daily walks sum `getattrlist(ATTR_CMNEXT_PRIVATESIZE)` for regular
  files, deduplicate hardlinks within each root, do not follow directory
  symlinks or cross devices. Block allocation is supplemental only. Shared
  extents and directory metadata are excluded; numbers are **not reclaim**.
  Missing, denied or timeout-partial roots have `bytes: null`, never zero.
- Roots: Codex sessions/whole home, Claude projects/VM/host state, Wispr live
  and backups, Documents/Codex, user/app caches, PWE local. Dynamic roots add
  current-user T/C/X (C/X from T's parent when necessary), `/private/tmp/claude-*`,
  `*-wt-*` folders under Development and PWE `local/<campaign>/<lane>` folders.
  Worktree discovery skips `.git`, node_modules, venvs, vendor, local and
  caches; this is partial discovery, not a complete git worktree registry.
  Chrome code-sign clone directory count is recorded separately under X.
- Per-root 180-second and total 1,800-second walk budgets. Discovery has its own
  180-second budget for Development. One daily lock prevents overlapping runs.
  A single blocking OS call can exceed the cooperative budget. Roots overlap;
  never sum the ranked changes as a disk-wide total. New/removed roots are not
  assigned invented deltas. Comparisons use the latest prior UTC date.

## Outputs and health

Private state lives under `STORAGE_STATE`, default
`~/.local/state/power-toolkit-storage`:

- `monitor/status.json`: fresh UTC, observing/degraded, free bytes, reasons,
  counter coverage, memory guardian return code and dispatch age. Old timestamps
  mean no fresh observation, not known health. Partial process coverage is
  degraded. The main loop dispatches the original guardian on its configured
  cadence, with at most one guardian child. A single daemon telemetry worker
  owns sampling, capture and state writes; blocked capture does not gate later
  guardian dispatches or accumulate workers. State initialization/write errors
  are reported separately and retried. No state write precedes guardian
  scheduling. Exclusion uses a read-only lock on the permanent Python helper;
  do not replace that checkout while its runner is active. This is ordinary OS
  thread scheduling, not a real-time deadline. `--once` is diagnostic and waits
  for its single sample. Capture/native reads may exceed 30 s and leave stale
  status while guardian dispatch continues; corroborate it from guardian logs.
- `monitor/ring.json`: 25 recent sample summaries, atomically replaced.
- `monitor/incident-00.json` through `incident-39.json`: circular slots replaced
  atomically. There is no automatic file deletion, cache clearing, thinning,
  pause or kill operation. Old incident content is overwritten after 40 captures.
- `daily/YYYY-MM-DD.json`, `.md`, and `latest.md`: prior-day growth, top 20 lane
  deltas, swap/VM trends, APFS role accounting, retained incident candidates and
  unavailable coverage. Historical daily files accumulate; no retention deletion.
- `last-error.json` in the selected output directory: down/phase/type/errno for
  fatal errors, or telemetry-unavailable for recoverable monitor errors. With an invalid config and no explicit output, stderr/nonzero
  exit is the fallback because the configured state location is unknown.
  Compare error and status timestamps; an old failure is not current health.
  When writes fail, structured stderr is the fallback; it contains only error
  type, phase, errno and UTC, never the exception text or sampled metadata.

Native metrics include APFS role sizes, snapshot counts, swap-used bytes and
Foundation capacity estimates. Unsupported/denied metrics are unavailable.
I/O counters, cwd and ancestry identify **candidates**, not per-file causation
or exact physical APFS changes. Exited/short-lived processes and shared/external
writers are incomplete; first sample has no interval writer baseline. No
notifications are independently emitted by the tracker: fast edges go through
`MEM_STORAGE_FAST_FALL=1` into the existing guardian's notifier/lock.

## Review, install and rollback (operator/root only)

Before installing, run the deterministic suites in CONTRIBUTING.md and inspect:

```zsh
zsh ./install.sh --dry-run --output /tmp/pt-reviewed-plists
plutil -lint /tmp/pt-reviewed-plists/*.plist
```

This mode exits before brew, chmod, symlink, pmset or launchctl. Normal
`zsh ./install.sh` is **not** a storage-only installer: it retains the toolkit's
existing dependency, symlink, pmset and other-agent reconciliation behavior.
After the merge/adoption gate, run `zsh ./install.sh` from the permanent checkout,
not a lane/worktree that will disappear. `pm apply` regenerates both jobs on
subsequent config changes. Store original plists/config before the gate.

Confirm the same memory label points at `bin/storage-tracking monitor`, the
daily label is loaded, and `monitor/status.json` UTC advances across two reads
10–15 seconds apart. Check guardian log progression at its configured cadence,
daily output, and actual notification delivery in a separately authorized test.
Run `bin/storage-tracking daily` manually if an initial baseline is needed.
Recheck after login/reboot and the next daily schedule. Passing fixtures does
not establish launchd, permissions/TCC, reboot behavior or notification delivery.

Rollback: persist `STORAGE_ENABLE=0` in config, then `STORAGE_ENABLE=0 bin/pm apply`.
It replaces the daily plist with `Disabled=true`, `RunAtLoad=false`, no calendar
and no KeepAlive, and boots out its loaded label. The same memory label becomes
the stock periodic shell guardian. Stock apply requires no Python. Both the
shell daily launcher and Python daily entry return before output/discovery when
disabled. All tracker state/history remains. Confirm original guardian cadence,
then login/reboot persistence and no new daily output. Do not overwrite unrelated
config changes or remove user data.

Re-enable: restore `STORAGE_ENABLE=1` in the permanent config and run `bin/pm apply`
after the operator gate. It replaces the disabled definition with the configured
daily schedule and reloads both storage labels. This route does not change
launchd's external disabled override database: capture that state before adoption
and resolve an existing override under operator authority before expecting a
bootstrap to start daily work. Do not enable an unrelated preexisting override.

## Apply failures and recovery

`pm apply` stages/validates both storage definitions before publication, snapshots
prior bytes/modes and loaded flags (unknown service state aborts), then publishes and reconciles both labels.
Failures on either publication, memory/daily reload or disabled-daily stop return
nonzero and attempt to restore prior persistent bytes/modes and loaded/unloaded
state. `pm install` propagates this failure. Generation itself stages both
members before publishing and restores prior files on a failed second rename.

Private `.storage-apply.*` and `.storage-generate-*` recovery bundles are retained
beside the generated plists; there is no automatic cleanup. Files absent before a
failed transaction are moved back into the recovery bundle, not deleted.
`.storage-apply.lock` excludes concurrent applies. A crash retains the lock and
phase/intent markers. Inspect the named bundle and the actual loaded definitions
before manually restoring config/jobs and removing only the stale empty lock.
If rollback itself fails, stderr reports incomplete restoration and the bundle
remains for operator recovery. Two file renames plus launchd calls are not one
crash-atomic transaction. Prior loaded definitions are assumed to match the
saved persistent plists; capture/compare the baseline before adoption.

The transaction covers the storage pair. The full toolkit install/apply still
has its existing dependency, symlink, pmset, CPU/saver/sleepwatcher behavior; this
is not a general toolkit-wide transactional installer. Native zsh/plutil/service
callback behavior and recovery from process/power loss require Mac qualification.
