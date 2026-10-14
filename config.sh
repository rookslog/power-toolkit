#!/bin/zsh
# power-toolkit — SINGLE SOURCE OF TRUTH.
# Sourced by `pm`, the watchdog, and the saver. Edit values here, then `pm apply`.
# PT_ROOT is exported by whichever entry script sources this file.

: ${PT_ROOT:?config.sh must be sourced with PT_ROOT set}
PT_VAR="$PT_ROOT/var"
PT_LOG="$PT_VAR/log"
PT_MODE=$(cat "$PT_VAR/mode" 2>/dev/null || print balanced)

PT_LABEL_PREFIX="${PT_LABEL_PREFIX:-local.powertoolkit}"
WATCHDOG_LABEL="$PT_LABEL_PREFIX.cpu-watchdog"
SAVER_LABEL="$PT_LABEL_PREFIX.power-saver"
SLEEPWATCHER_LABEL="$PT_LABEL_PREFIX.sleepwatcher"
MEM_WATCHDOG_LABEL="$PT_LABEL_PREFIX.mem-watchdog"

# ---------- kill-safety (GOAL_CONTRACT §4) — ONE list, consumed by BOTH watchdogs ----------
# Never SIGTERM/SIGKILL, whatever the CPU or RSS. Defined once, here, because the two watchdogs
# previously carried SEPARATE lists and they drifted: the CPU path's `KILL_ALLOWLIST` omitted
# claude/codex/cmux, Finder and com.apple.Virtualization, all of which §4 names as untouchable
# and all of which the memory path already protected. Consequence: in `aggressive` mode
# (AUTO_KILL=1) the cpu-watchdog would SIGTERM a sustained-hot LIVE Claude Code session — a
# shipped §4 violation. Found by cross-vendor review 2026-07-26 (review-journal/0001, F4; E-025).
# Derive both consumers from these two, never re-list members inline.
PROTECT_SYSTEM="kernel_task WindowServer launchd loginwindow coreaudiod hidd powerd bluetoothd com.apple.Virtualization Finder mds mds_stores mdworker backupd configd securityd opendirectoryd logd UserEventAgent"
PROTECT_SESSION="claude codex cmux"   # live coding sessions — a guardian must never kill its own

# ---------- defaults (balanced mode) ----------
# CPU watchdog — catches processes pegging a core continuously (the pyenv-loop failure mode).
CPU_THRESHOLD=85                 # %CPU; ~100 == one core fully pegged
SUSTAIN_MIN=15                   # sustained minutes above threshold before alerting
WATCHDOG_INTERVAL_SEC=300        # how often the watchdog samples
AUTO_KILL=0                      # 1 = SIGTERM offenders (allowlist always protected)
KILL_ALLOWLIST="$PROTECT_SYSTEM $PROTECT_SESSION"
# Known heavy-but-legit system daemons (indexing / media analysis / backup) — never even alert;
# they routinely peg a core doing self-limiting batch work, usually only on AC.
ALERT_ALLOWLIST="mediaanalysisd photoanalysisd photolibraryd cloudphotod mds mds_stores mdworker mdworker_shared Spotlight backupd XProtect XprotectService corespeechd amplibraryagent AMPDeviceDiscoveryAgent"

# ---------- aggregate ("distributed runaway") gate ----------
# The per-process gate above is structurally blind to MANY small hogs that each stay under
# CPU_THRESHOLD. Observed 2026-07-26: 36 orphaned busy-loops at ~22% CPU each — ~800% aggregate,
# load 142 on 10 cores, 0.0% idle for ~9h — and every one was filtered out at line `cpu >=
# CPU_THRESHOLD`, so zero alerts fired. This gate watches the SYSTEM, not any single process.
# DETECTION-ONLY BY DESIGN: an aggregate signal cannot attribute blame (36 orphans and a
# legitimate `make -j16` share a load signature), so it must never kill. Attributed-kill is a
# separate, profile-classified design — see goal/briefs/0002 and GOAL_CONTRACT §4 (notify-first).
AGG_ENABLE=1
AGG_LOAD_PER_CORE=2.5            # 1-min load avg / ncpu above this = oversubscribed
AGG_SUSTAIN_MIN=10               # sustained minutes above threshold before alerting
AGG_TOP_N=5                      # how many top contributor groups to name in the alert
AGG_MIN_PROC_CPU=1               # ignore procs below this %CPU when grouping (cost control:
                                 # skips ~850 idle procs/tick; they cannot cause an overload)
AGG_ALLOWLIST_PCT=60             # if at least this %% of the summed load is ALERT_ALLOWLIST batch
                                 # work (Spotlight, backupd, mediaanalysisd...), log AGG-EXPECTED
                                 # and stay quiet. Without this the aggregate gate re-reported the
                                 # very indexing workloads ALERT_ALLOWLIST exists to silence.

# ---------- memory guardian (mem-watchdog) ----------
# Adaptive, pressure-aware RSS watchdog over ALL processes (the rg-to-17GB swap-exhaustion
# failure mode). Risk model + profiles: see goal/GOAL_CONTRACT.md §3.
# M0 remains OBSERVE-ONLY for processes: logs risk/would-actions; never signals a process.
# System threshold notifications are independent of the per-process decision ladder.
MEM_WATCHDOG_INTERVAL_SEC=60     # base poll cadence (one `ps` per tick when healthy)
MEM_OBSERVE=1                    # per-process observe-only (M0); no signal code path exists.
MEM_TOP_N=5                      # per tick, log this many highest-risk processes
MEM_DATA_VOLUME="/System/Volumes/Data"  # one O(1) df -k lookup, never a directory walk
MEM_DISK_LOW_BYTES=$(( 10 * 1024 * 1024 * 1024 ))  # below 10 GiB available to the user
MEM_SWAP_HIGH_BYTES=8000000000    # above 8 GB (decimal); separate from risk/kill eligibility
MEM_NOTIFY_ENABLE=1              # edge-triggered system alerts + recoveries, best-effort async

# Headroom gate — danger is HEADROOM-AWARE, never a flat per-process RSS ceiling.
# Primary signal is the kernel's own verdict + free-memory %, NOT absolute swap (this machine
# can sit at >12G swap-used without thrashing; absolute swap is a poor instantaneous gate).
MEM_FREE_CAUTION_PCT=25          # kern.memorystatus_level (free %) below this => caution
MEM_FREE_CRITICAL_PCT=12         # free % below this => critical
# Kernel pressure level (sysctl kern.memorystatus_vm_pressure_level): 1 normal / 2 warn / 4 critical.
# headroom_state = worse of (free-%% band, kernel pressure band). swap delta is logged, not gated (M0).

# Risk model:  risk = w1*(rss/baseline) + w2*growth_norm + w3*(1/tolerance) - w4*fg_trusted
MEM_W_SIZE=0.5                   # w1: RSS relative to its profile's seeded baseline
MEM_W_GROWTH=3.0                 # w2: rapid growth dominates (the rg signature)
MEM_W_PROFILE=1.0                # w3: inverse profile tolerance (low-tolerance => higher risk)
MEM_W_FOREGROUND=1.5             # w4: discount for trusted .app processes
MEM_GROWTH_REF_MBPS=20           # MB/s of growth that normalizes to growth_norm = 1.0
MEM_RISK_WATCH=1.5               # risk >= this => log a detail line (watch), any headroom
MEM_RISK_NOTIFY=3.0              # risk >= this in CAUTION headroom => would-notify
MEM_RISK_KILL=5.0                # risk >= this in CRITICAL headroom => would-kill (M1+; M0 logs only)
# A would-notify/would-kill additionally requires SUSTAINED growth (not a single noisy tick —
# legit apps burst-allocate on page loads / rendering), so a stable heavy app or a transient
# burst is never flagged for action. (D-008, mirrors the cpu-watchdog SUSTAIN pattern.)
MEM_GROWTH_MIN_MBPS=2            # a tick counts as "growing" at/above this rate…
MEM_SUSTAIN_TICKS=3             # …and must grow this many CONSECUTIVE ticks to be action-eligible…
MEM_RATIO_HARD=10               # …unless RSS already exceeds baseline by this factor (already-runaway).

# Profiles — RELATIVE tolerances + seeded baselines (M2 replaces baselines with learned p50/p95).
#   tol   = relative tolerance (higher = more RSS is "normal" for this class)
#   base  = seeded typical RSS (MB), the denominator of the size ratio
MEM_SYSTEM_TOL=1000;     MEM_SYSTEM_BASE=4096      # protected; never acted on regardless of RSS
MEM_TRUSTED_TOL=20;      MEM_TRUSTED_BASE=2048     # signed .app — high tolerance, notify-first
MEM_DEVRUNTIME_TOL=6;    MEM_DEVRUNTIME_BASE=1024  # node/python/etc under a workflow — medium
MEM_EPHEMERAL_TOL=2;     MEM_EPHEMERAL_BASE=128    # bare CLI (rg) — low; auto-kill-eligible in M1
MEM_UNKNOWN_TOL=3;       MEM_UNKNOWN_BASE=256      # conservative; notify-first until learned
MEM_AUTOKILL_PROFILES="ephemeral"                  # which classes M1 may auto-kill (opt-in)

# Classification (substring/basename match on the full exec path from `ps -o comm`):
# Protected/system + live sessions — the kill-safety list (GOAL_CONTRACT §4). NEVER signalled
# regardless of RSS. Derived from the canonical lists at the top of this file so the CPU and
# memory paths cannot drift apart again (that drift was the F4 bug).
MEM_PROTECT="$PROTECT_SYSTEM"
MEM_SESSION_PROTECT="$PROTECT_SESSION"
# dev runtimes (medium tolerance) — matched by path basename.
MEM_DEVRUNTIME="node deno bun python python3 ruby java cargo rustc clangd gopls tsserver"
# ephemeral CLIs (low tolerance; kill-eligible in M1) — matched by path basename.
MEM_EPHEMERAL="rg grep egrep fgrep ag ack find fd sort uniq awk sed tail xargs jq tr wc"

# Bluetooth-on-sleep saver — drop BT only if the lid has been shut a while (keeps short room-moves snappy).
BT_DROP_AFTER_MIN=30             # lid closed this long -> turn Bluetooth off
BT_DROP_ON_BATTERY_ONLY=1        # 1 = only act when unplugged
SAVER_INTERVAL_SEC=300           # how often the saver ticks

# pmset desired state — "profile setting value"; profile: b=battery, c=AC, a=all.
# `pm apply` reconciles actual pmset to this list (needs sudo).
PMSET_DESIRED=(
  "b tcpkeepalive 0"
  "b powernap 0"
  "c tcpkeepalive 1"
  "c powernap 1"
)

# ---------- log rotation — shared by BOTH watchdogs ----------
# The agents run indefinitely and append every tick, with no bound. Measured 2026-07-26:
# var/log/mem-watchdog.log had reached 4.3 MB in roughly five weeks of observe mode (~45 MB/yr),
# and `pm status` greps these files on every invocation, so they get slower as they grow.
# Defined here rather than in either watchdog because both have the defect and a second copy is
# a second thing to drift (the KILL_ALLOWLIST lesson).
PT_LOG_MAX_KB=${PT_LOG_MAX_KB:-5120}     # rotate past this size; one .1 generation is kept,
                                         # so worst case on disk is 2x this per log.
pt_rotate_log() {                        # $1 = log path
  [[ -f "$1" ]] || return 0
  local bytes; bytes=$(wc -c < "$1" 2>/dev/null) || return 0
  (( bytes / 1024 >= PT_LOG_MAX_KB )) || return 0
  mv -f "$1" "$1.1" 2>/dev/null
  # Recreate it. An ordinary tick writes nothing to these logs — only alerts, policy changes and
  # gaps do — so without this the active path stays ABSENT until the next such event, and both
  # `pm status` and `pm logs` (which read only the active file) go blind right after a rotation.
  : > "$1" 2>/dev/null
}

# ---------- mode overrides ----------
case "$PT_MODE" in
  aggressive)
    AUTO_KILL=1
    BT_DROP_AFTER_MIN=15
    PMSET_DESIRED+=( "b womp 0" "b standby 1" )
    MEM_WATCHDOG_INTERVAL_SEC=30             # poll twice as often
    AGG_LOAD_PER_CORE=2.0                    # flag oversubscription sooner
    AGG_SUSTAIN_MIN=5
    # NB: M0 is observe-only in every mode — MEM_OBSERVE stays 1 until M1 ships kill code.
    # NB: the aggregate gate is detection-only in EVERY mode, including aggressive — AUTO_KILL
    # governs the per-process path only. It has no kill code path at all (brief 0002).
    ;;
  conservative)
    AUTO_KILL=0
    BT_DROP_AFTER_MIN=60
    PMSET_DESIRED=( "b tcpkeepalive 0" )     # minimal touch
    MEM_WATCHDOG_INTERVAL_SEC=120
    AGG_LOAD_PER_CORE=4.0                    # only flag severe oversubscription
    AGG_SUSTAIN_MIN=20
    ;;
  balanced|*) ;;                             # use defaults above
esac
