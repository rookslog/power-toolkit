# Contributing

Issues and PRs are welcome. No CLA required — this is a small, personal-scale macOS power-management toolkit and contributions should stay in that spirit.

## Language and style

- All scripts are written in **zsh**. Match the existing style and keep changes minimal and focused.
- Do not introduce bash-isms or POSIX-sh patterns; this repo targets zsh on macOS.

## Configuration

- `config.sh` is the single source of truth for all configurable values.
- Do not hardcode paths, thresholds, or other settings inside `bin/pm`, `install.sh`, or `libexec/*`. Add or update values in `config.sh` instead.

## Before opening a PR

1. Run `zsh -n` on every script and confirm there are no syntax errors:

   ```zsh
   for f in bin/pm install.sh config.sh libexec/*; do
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
