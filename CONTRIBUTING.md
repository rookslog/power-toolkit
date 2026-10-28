# Contributing

Issues and PRs are welcome. No CLA required — this is a small, personal-scale macOS power-management toolkit and contributions should stay in that spirit.

## Language and style

- Entry points and guardians use **zsh**. Storage metadata helpers use standard-library Python 3.9+ for ctypes and structured reports; no pip dependencies. Match the existing style and keep changes focused.
- Do not introduce bash-isms or POSIX-sh patterns; this repo targets zsh on macOS.

## Configuration

- `config.sh` is the single source of truth for all configurable values.
- Do not hardcode paths, thresholds, or other settings inside `bin/pm`, `install.sh`, or `libexec/*`. Add or update values in `config.sh` instead.

## Before opening a PR

1. Run `zsh -n` on every script and confirm there are no syntax errors:

   ```zsh
   for f in bin/* install.sh config.sh libexec/*; do
     [[ "$f" == *.py || -d "$f" ]] && continue
     echo "checking $f"
     zsh -n "$f"
   done
   ```

2. Run `pm doctor` and confirm it exits cleanly.

Both checks must be clean before the PR is opened. The CI workflow (`lint.yml`) enforces the `zsh -n` gate automatically.

3. Run `zsh tests/test-mem-notifications.sh`. This deterministic suite uses isolated
   state and synthetic system commands/notifier, never real load or notifications.
   CI also runs it. Do not use a real-load stress test as a routine pre-PR check.

## Note on shellcheck

shellcheck is **not used** in this project. shellcheck targets POSIX sh/bash and produces false positives on valid zsh syntax. `zsh -n` is the syntax gate here.

## Storage checks (no deployment)

```zsh
zsh tests/test-fast-fall.sh
PT_RUN_MAC_INTEGRATION=1 STORAGE_PYTHON="$(command -v python3)" python3 -B -m unittest discover -s tests -p 'test_storage*.py' -v
zsh ./install.sh --dry-run --output /tmp/pt-reviewed-plists
```

Dry-run writes only the supplied directory: no brew, chmod, symlink, pmset or
launchctl. Unknown installer arguments fail before side effects. See
[storage tracking](docs/storage-tracking.md) for adoption and rollback. `pm doctor`
is a post-install read-only check, not a substitute for deterministic fixtures.

Linux source/fixture checks (no installer or service entry points):

```sh
python3 -B -m unittest discover -s tests -p 'test_storage*.py' -v
```

Five original methods require zsh or explicit macOS fixture admission; missing
prerequisites produce explicit skips. `PT_RUN_MAC_INTEGRATION=1` enables the copied installer/apply fixtures
only on macOS. Inspect their local paths and mutator substitutes before enabling
that flag. Transaction-library tests on Linux source only `storage-jobs.sh` in
Bash with substituted generation, validation and service callbacks. They do not
qualify the zsh/native `pm` glue, launchd or login persistence. CI's macOS job
opts in to the copied-tree fixtures.
