# Source-only storage job transaction, used by pm. No commands run on sourcing.
# Keep the transaction subset compatible with zsh and the Linux fixture shell.
# Callbacks: storage_prepare DIR, storage_validate DIR, agent_loaded LABEL,
# reload_agent LABEL PLIST, stop_agent LABEL. agent_loaded returns 0 loaded,
# 1 absent, 2 unknown/error. Configuration comes from config.sh.

storage_restore_files() {
  local txn=$1 label failed=0
  for label in "$MEM_WATCHDOG_LABEL" "$STORAGE_DAILY_LABEL"; do
    if [[ -f "$txn/published-$label" ]]; then
      if [[ -f "$txn/prior/$label.plist" ]]; then
        mv -f "$txn/prior/$label.plist" "$AGENTS_DIR/$label.plist" || failed=1
      elif [[ -f "$AGENTS_DIR/$label.plist" ]]; then
        # Restore prior absence by retaining the rejected config in the bundle.
        mv -f "$AGENTS_DIR/$label.plist" "$txn/rejected-$label.plist" || failed=1
      fi
    fi
  done
  return "$failed"
}

storage_restore_loaded() {
  local txn=$1 label failed=0
  for label in "$MEM_WATCHDOG_LABEL" "$STORAGE_DAILY_LABEL"; do
    if [[ -f "$txn/loaded-$label" ]]; then
      reload_agent "$label" "$AGENTS_DIR/$label.plist" || failed=1
    else
      stop_agent "$label" || failed=1
    fi
  done
  return "$failed"
}

storage_apply_locked() {
  local txn=$1 label state_rc
  mkdir -p "$txn/stage" "$txn/prior" || return 1
  storage_prepare "$txn/stage" || return 1
  storage_validate "$txn/stage" || return 1
  # Prepare all backups before the first persistent publication/service change.
  for label in "$MEM_WATCHDOG_LABEL" "$STORAGE_DAILY_LABEL"; do
    [[ -f "$txn/stage/$label.plist" ]] || return 1
    if [[ -e "$AGENTS_DIR/$label.plist" ]]; then
      cp -p "$AGENTS_DIR/$label.plist" "$txn/prior/$label.plist" || return 1
    fi
    if agent_loaded "$label"; then
      : > "$txn/loaded-$label" || return 1
    else
      state_rc=$?
      (( state_rc == 1 )) || return 1
    fi
  done
  printf '%s\n' prepared > "$txn/phase" || return 1
  for label in "$MEM_WATCHDOG_LABEL" "$STORAGE_DAILY_LABEL"; do
    # Record intent before rename so a crash leaves a recoverable transaction.
    if ! { : > "$txn/published-$label" &&
           mv -f "$txn/stage/$label.plist" "$AGENTS_DIR/$label.plist"; }; then
      storage_restore_files "$txn" || printf '%s\n' "storage file rollback incomplete: $txn" >&2
      return 1
    fi
  done
  if ! { reload_agent "$MEM_WATCHDOG_LABEL" "$AGENTS_DIR/$MEM_WATCHDOG_LABEL.plist" &&
         if (( STORAGE_ENABLE )); then
           reload_agent "$STORAGE_DAILY_LABEL" "$AGENTS_DIR/$STORAGE_DAILY_LABEL.plist"
         else
           stop_agent "$STORAGE_DAILY_LABEL"
         fi && printf '%s\n' committed > "$txn/phase"; }; then
    if storage_restore_files "$txn"; then
      storage_restore_loaded "$txn" || printf '%s\n' "storage service rollback incomplete: $txn" >&2
    else
      printf '%s\n' "storage file rollback incomplete; service restore withheld: $txn" >&2
    fi
    return 1
  fi
  return 0
}

storage_apply() {
  local txn rc
  umask 077
  mkdir -p "$AGENTS_DIR" || return 1
  # Crash leaves the lock and private recovery bundle for operator inspection.
  # A concurrent apply refuses to publish rather than racing another transaction.
  mkdir "$AGENTS_DIR/.storage-apply.lock" 2>/dev/null || {
    printf '%s\n' 'storage apply locked; inspect the prior recovery bundle' >&2
    return 1
  }
  txn=$(mktemp -d "$AGENTS_DIR/.storage-apply.XXXXXXXX") || {
    rmdir "$AGENTS_DIR/.storage-apply.lock"
    return 1
  }
  storage_apply_locked "$txn"
  rc=$?
  printf '%s\n' "storage apply exit $rc; recovery bundle: $txn" >&2
  rmdir "$AGENTS_DIR/.storage-apply.lock" || return 1
  return "$rc"
}

# Python-independent rollback definition. No timer, RunAtLoad or KeepAlive work.
storage_disabled_daily() {
  # Labels are config identifiers; reject XML metacharacters before output.
  case "$STORAGE_DAILY_LABEL" in
    *[!A-Za-z0-9._-]*|'') return 1 ;;
  esac
  cat > "$1" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$STORAGE_DAILY_LABEL</string>
<key>ProgramArguments</key><array><string>/usr/bin/true</string></array>
<key>Disabled</key><true/>
<key>RunAtLoad</key><false/>
</dict></plist>
EOF
}
