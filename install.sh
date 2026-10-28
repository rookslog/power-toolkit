#!/bin/zsh
# Bootstrap: install deps (if brew present), then hand off to `pm install`.
emulate -L zsh
HERE=${0:A:h}
# Handle isolated generation before dependencies, chmod, pmset or launchctl.
if (( $# )); then
  if [[ "$1" == --dry-run && "${2:-}" == --output && $# == 3 && -n "$3" ]]; then
    exec "$HERE/bin/storage-tracking" generate-plists --output "$3"
  fi
  print -u2 -- 'usage: install.sh [--dry-run --output DIRECTORY]'
  exit 2
fi
chmod +x "$HERE/bin/pm" "$HERE"/libexec/*(.) 2>/dev/null
if command -v brew >/dev/null; then
  for t in blueutil sleepwatcher; do
    command -v $t >/dev/null || brew install $t
  done
fi
exec "$HERE/bin/pm" install
