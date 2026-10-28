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
expect 'healthy start is quiet' "$(events)" 0
grep -q 'data_free_bytes=21474836480' "$T/log" && ok 'TICK has Data available bytes' || no 'TICK has Data available bytes'
readings 4 9437184 9G; tick
expect 'three independent entry edges' "$(events)" 3
await_done 1
expect 'entry edges batched into one notification' "$(count "$T/notices")" 1
grep -q 'swap_bytes=9663676416' "$T/log" && ok 'swap G converted to bytes' || no 'swap G converted to bytes'
tick
expect 'unchanged incident never repeats across invocations' "$(events)" 3
expect 'no repeated notification' "$(count "$T/notices")" 1

readings missing missing missing; tick
expect 'unknown reads invent no recoveries' "$(events)" 3
tail -1 "$T/log" | grep -q 'data_free_bytes=?' && ok 'unknown disk is not zero' || no 'unknown disk is not zero'
tail -1 "$T/log" | grep -q 'swap=?M' && ok 'unknown swap is not zero' || no 'unknown swap is not zero'
# Valid-looking stdout with a failing command must still count as unavailable.
export MOCK_READ_EXIT=1
readings 1 20971520 0M; tick
expect 'failed command with numeric stdout invents no recovery' "$(events)" 3
unset MOCK_READ_EXIT
readings 0 9437184 9G; tick
expect 'unsupported pressure zero invents no recovery' "$(events)" 3
readings 2 9437184 9G; tick
expect 'critical to warning is pressure recovery' "$(events)" 4
await_done 2
readings 1 10485760 8000000000; tick
expect 'disk equality and swap equality recover' "$(events)" 6
await_done 3
tick
expect 'healthy state stays quiet' "$(events)" 6
readings 1 10485759 8000000001; tick
expect 'one KiB below 10 GiB and one byte above 8 GB enter' "$(events)" 8
await_done 4
readings 1 10485760 8000000000; tick
expect 'exact boundaries recover both signals' "$(events)" 10
await_done 5

# Notification failure must not re-notify an unchanged condition every minute.
export MOCK_NOTIFY_EXIT=7
readings 4 20971520 0M; tick; await_done 6
sleep 0.05
tick
expect 'failed notifier is not retried on same state' "$(count "$T/notices")" 6
grep -q 'NOTIFY-ERROR.*7' "$T/log" && ok 'notification failure observable' || no 'notification failure observable'
unset MOCK_NOTIFY_EXIT

# Slow external delivery cannot hold the tick or accumulate detached workers.
readings 1 20971520 0M; tick; await_done 7
export MOCK_NOTIFY_DELAY=2
zmodload zsh/datetime
readings 4 20971520 0M
before=$EPOCHREALTIME; tick; elapsed=$(( EPOCHREALTIME - before ))
(( elapsed < 1 )) && ok 'slow notifier never blocks tick' || no 'slow notifier never blocks tick' "$elapsed seconds"
for i in {1..100}; do [[ "$(count "$T/notices")" == 8 ]] && break; sleep 0.02; done
readings 1 9437184 0M; tick
sleep 0.1
expect 'busy notifier does not accumulate workers' "$(count "$T/notices")" 8
grep -q 'NOTIFY-SKIPPED.*busy' "$T/log" && ok 'busy delivery explicitly logged' || no 'busy delivery explicitly logged'
await_done 8
unset MOCK_NOTIFY_DELAY
# A delayed dispatcher must finish detaching before the launchd parent can exit.
# Actual Perl still performs detachment/fork; the wrapper only delays its startup.
export MEM_DETACH_COMMAND_OVERRIDE="$T/bin/detacher"
readings 1 20971520 0M; tick
expect 'detachment startup completes before tick returns' "$(count "$T/detacher-returned")" 1
await_done 9
# Perl wait status 15 means signal 15, not successful exit 0. Exercise the real decoder
# with a synthetic OS boundary result; do not terminate a real notifier for this test.
export MOCK_SYSTEM_STATUS=15
readings 4 20971520 0M; tick
for i in {1..100}; do grep -q 'NOTIFY-ERROR signal=15' "$T/log" && break; sleep 0.02; done
grep -q 'NOTIFY-ERROR signal=15' "$T/log" && ok 'signal termination is not reported as exit zero' || no 'signal termination is not reported as exit zero' "$(grep 'NOTIFY-ERROR' "$T/log" | tail -1)"
unset MOCK_SYSTEM_STATUS
unset MEM_DETACH_COMMAND_OVERRIDE
parent_group=$(/usr/bin/perl -e 'print getpgrp()')
worker_group=$(head -1 "$T/groups")
[[ "$worker_group" == <-> && "$parent_group" != "$worker_group" ]] && ok 'notifier detached from caller process group' || no 'notifier detached from caller process group'
expect 'one process snapshot per tick' "$(count "$T/scans")" 18
expect 'no tick stderr errors' "$(count "$T/errors")" 0
expect 'observe-only never attempts a process signal' "$(count "$T/signals")" 0
print "RESULT: $pass passed, $fail failed"
exit $((fail>0))
