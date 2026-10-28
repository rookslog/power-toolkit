#!/bin/zsh
emulate -L zsh
set -u
# Synthetic readings through the real watchdog. No live state, notifications or signals.
ROOT=${0:A:h:h}
T=$(mktemp -d "${TMPDIR:-/tmp}/pt-notify-test.XXXXXX")
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/zdot"
export MOCK_DIR="$T" SIGNAL_LOG="$T/signals"
cat > "$T/zdot/.zshenv" <<'EOF'
kill() { print signal-attempt >> "$SIGNAL_LOG"; return 1; }
EOF
cat > "$T/bin/sysctl" <<'EOF'
#!/bin/zsh
case "$2" in
 kern.memorystatus_vm_pressure_level) cat "$MOCK_DIR/pressure";;
 kern.memorystatus_level) print 80;;
 vm.swapusage) print "total = 32000M used = $(cat "$MOCK_DIR/swap") free = 0M";;
 *) exit 1;;
esac
exit "${MOCK_READ_EXIT:-0}"
EOF
cat > "$T/bin/df" <<'EOF'
#!/bin/zsh
[[ "$*" == '-Pk /System/Volumes/Data' ]] || exit 9
print 'Filesystem 1024-blocks Used Available Capacity Mounted on'
print "fixture 100000000 1 $(cat "$MOCK_DIR/free_kb") 1% /System/Volumes/Data"
exit "${MOCK_READ_EXIT:-0}"
EOF
cat > "$T/bin/ps" <<'EOF'
#!/bin/zsh
[[ "$*" == '-axo pid=,rss=,etime=,comm=' ]] || exit 9
print scan >> "$MOCK_DIR/scans"
print '99001 1024 10:00 /bin/fixture'
EOF
cat > "$T/bin/notifier" <<'EOF'
#!/bin/zsh
print -- "$argv[-1]" >> "$MOCK_DIR/notices"
/usr/bin/perl -e 'print getpgrp()."\n"' >> "$MOCK_DIR/groups"
sleep "${MOCK_NOTIFY_DELAY:-0}"
print done >> "$MOCK_DIR/done"
exit "${MOCK_NOTIFY_EXIT:-0}"
EOF
cat > "$T/bin/detacher" <<'EOF'
#!/bin/zsh
sleep 0.25
if [[ -n "${MOCK_SYSTEM_STATUS:-}" ]]; then
 /usr/bin/perl -I "$MOCK_DIR" -MWaitStatusFixture "$@"
else
 /usr/bin/perl "$@"
fi
rc=$?
print ready >> "$MOCK_DIR/detacher-returned"
exit $rc
EOF
cat > "$T/WaitStatusFixture.pm" <<'EOF'
package WaitStatusFixture;
# Substitute only the external system() wait result; production decoder runs unchanged.
# No process receives a signal to manufacture this failure case.
BEGIN { *CORE::GLOBAL::system = sub (&@) { $? = int($ENV{MOCK_SYSTEM_STATUS}); return $?; }; }
1;
EOF
chmod +x "$T/bin/"*
pass=0; fail=0
ok(){ print "PASS $1"; ((pass+=1)); }
no(){ print "FAIL $1: ${2:-unexpected result}"; ((fail+=1)); }
expect(){ [[ "$2" == "$3" ]] && ok "$1" || no "$1" "want=$3 got=$2"; }
count(){ [[ -f "$1" ]] && wc -l < "$1" | tr -d ' ' || print 0; }
events(){ grep -c ' EVENT ' "$T/log" || true; }
tick(){
 PATH="$T/bin:$PATH" ZDOTDIR="$T/zdot" MEM_LOG_OVERRIDE="$T/log" \
 MEM_STATE_OVERRIDE="$T/process-state" MEM_SWAPLAST_OVERRIDE="$T/swap-last" \
 MEM_ALERT_STATE_OVERRIDE="$T/alerts" MEM_NOTIFY_LOCK_OVERRIDE="$T/notify.lock" \
 MEM_NOTIFY_COMMAND_OVERRIDE="$T/bin/notifier" zsh "$ROOT/libexec/mem-watchdog.sh" 2>> "$T/errors"
}
await_done(){
 local want=$1 i
 for i in {1..100}; do [[ "$(count "$T/done")" -ge "$want" ]] && return 0; sleep 0.03; done
 no 'notification worker completes' "want=$want got=$(count "$T/done")"
}
readings(){ print -- "$1" > "$T/pressure"; print -- "$2" > "$T/free_kb"; print -- "$3" > "$T/swap"; }

readings 1 20971520 0M; tick
MEM_STORAGE_FAST_FALL=1 tick
await_done 1
grep -q 'disk:fast-fall' "$T/notices" && ok 'fast fall uses original notifier' || no 'fast fall uses original notifier'
grep -q 'EVENT disk:fast-fall' "$T/log" && ok 'fast fall persisted in original event log' || no 'fast fall persisted in original event log'
tick
expect 'no unsolicited repeated fast alert' "$(count "$T/notices")" 1
expect 'no stderr' "$(count "$T/errors")" 0
print "RESULT: $pass passed, $fail failed"
exit $((fail>0))
