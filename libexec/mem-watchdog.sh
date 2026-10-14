#!/bin/zsh
emulate -L zsh
set -u
setopt PIPE_FAIL
# Adaptive, pressure-aware memory guardian over ALL processes (the rg-to-17GB swap-exhaustion
# failure mode). Decides intervention by a HEADROOM-AWARE risk score + profile class, never a
# flat absolute RSS ceiling. Reads everything from config.sh.
#
# M0 = OBSERVE-ONLY: this script never signals any process. It logs, per tick, system headroom
# and — for each notable process — the risk score, profile, and the action it WOULD take. The
# kill code path is introduced in M1, after observe evidence shows the scores are sane.
#
# System threshold notifications do not change that per-process decision ladder.
# Steady-state cost: one `ps`, a few O(1) sysctls and one Data-volume `df` per tick.

PT_ROOT=${0:A:h:h}; source "$PT_ROOT/config.sh"
zmodload zsh/datetime 2>/dev/null
mkdir -p "$PT_LOG"
# Paths honor optional MEM_*_OVERRIDE env (unset in production / launchd) so the test harness
# can run in full isolation without touching the live observe-soak log or state.
LOG="${MEM_LOG_OVERRIDE:-$PT_LOG/mem-watchdog.log}"
STATE="${MEM_STATE_OVERRIDE:-$PT_VAR/mem-state.tsv}"   # pid \t rss_kb \t first_seen \t last_seen \t streak \t start
SWAPLAST="${MEM_SWAPLAST_OVERRIDE:-$PT_VAR/mem-swap-last}"   # last swap-used (MB), for the growth delta
ALERT_STATE="${MEM_ALERT_STATE_OVERRIDE:-$PT_VAR/mem-alert-state.tsv}"
NOTIFY_LOCK="${MEM_NOTIFY_LOCK_OVERRIDE:-$PT_VAR/mem-notify.lock}"
pt_rotate_log "$LOG"     # bound the observe history; see PT_LOG_MAX_KB in config.sh

ts() { strftime '%Y-%m-%d %H:%M:%S' $EPOCHSECONDS; }

# ---------- system headroom (all O(1): no per-process scan) ----------
# These are PRIVATE sysctls and can be missing or fail transiently. The old fallbacks were the
# healthiest possible values (press=1, free_pct=100), so a failed read was indistinguishable
# from a healthy machine: monitoring degraded to "everything is fine" and said nothing. Track
# availability explicitly instead, band only on the signals we actually have, and put the
# degradation in the log where it can be seen.
degraded=""
press=$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null) || press=""
free_pct=$(sysctl -n kern.memorystatus_level 2>/dev/null) || free_pct=""
swap_bytes=$(LC_ALL=C sysctl -n vm.swapusage 2>/dev/null | awk '
  { for(i=1;i<=NF;i++) if($i=="used" && $(i+1)=="=") {
      v=$(i+2); if(v !~ /^[0-9]+(\.[0-9]+)?[KMGT]?B?$/) exit
      unit=v; gsub(/[0-9.B]/,"",unit); sub(/[KMGT]?B?$/,"",v)
      scale=(unit=="K"?1024:unit=="M"?1048576:unit=="G"?1073741824:unit=="T"?1099511627776:1)
      printf "%.0f\n",v*scale; exit
  } }') || swap_bytes=""
data_free_bytes=$(LC_ALL=C df -Pk "$MEM_DATA_VOLUME" 2>/dev/null | awk '
  NR==2 && $4 ~ /^[0-9]+$/ { printf "%.0f\n", $4*1024 }') || data_free_bytes=""
[[ "$press" == 1 || "$press" == 2 || "$press" == 4 ]] || { degraded="${degraded}pressure "; press=""; }
[[ "$free_pct" == <-> ]] || { degraded="${degraded}free-pct "; free_pct=""; }
[[ "$swap_bytes" == <-> ]] || { degraded="${degraded}swap "; swap_bytes=""; }
[[ "$data_free_bytes" == <-> ]] || { degraded="${degraded}disk "; data_free_bytes=""; }

swap_used=""; swap_delta="?"
if [[ -n "$swap_bytes" ]]; then
  swap_used=$(( swap_bytes / 1048576 ))
  prev_swap=$(cat "$SWAPLAST" 2>/dev/null)
  [[ "$prev_swap" == <-> ]] || prev_swap=$swap_used
  printf -v swap_delta '%+d' $(( swap_used - prev_swap ))
  print -- "$swap_used" > "$SWAPLAST"
fi

rank() { case "$1" in critical) print 3;; caution) print 2;; unknown) print 1;; *) print 0;; esac }
# free-% band — `unknown` when the signal is unavailable, never `healthy`
if   [[ -z "$free_pct" ]];                    then band=unknown
elif (( free_pct <= MEM_FREE_CRITICAL_PCT )); then band=critical
elif (( free_pct <= MEM_FREE_CAUTION_PCT  )); then band=caution
else band=healthy; fi
# kernel pressure band
if [[ -z "$press" ]]; then pband=unknown
else case "$press" in 4) pband=critical;; 2) pband=caution;; *) pband=healthy;; esac; fi
# headroom_state = worse of the two. `unknown` outranks `healthy`, so a machine we cannot see
# never reports as fine; it does NOT outrank a real caution/critical read from the other signal.
if (( $(rank $pband) > $(rank $band) )); then headroom=$pband; else headroom=$band; fi
[[ -n "$degraded" ]] && print -- "$(ts) DEGRADED unavailable=${degraded%% } headroom=$headroom; unknown readings shown as ?" >> "$LOG"

# ---------- persistent system edges (unknown never invents a recovery) ----------
# launchd invokes a fresh process each tick, so in-memory state alone would repeat alerts.
typeset -A previous=(pressure normal disk normal swap normal)
if [[ -f "$ALERT_STATE" ]]; then
  while IFS=$'\t' read -r key value; do
    case "$key:$value" in
      pressure:normal|pressure:critical|disk:normal|disk:low|swap:normal|swap:high) previous[$key]=$value;;
    esac
  done < "$ALERT_STATE"
fi
typeset -a edges=()
edge() {
  local key=$1 value=$2
  [[ -n "$value" ]] || return 0
  if [[ "$value" != "${previous[$key]}" ]]; then
    local event="$key:$value"
    [[ "$value" == normal ]] && event="$key:recovered"
    edges+=("$event")
    print -- "$(ts) EVENT $event" >> "$LOG"
  fi
  previous[$key]=$value
}
pressure_alert=""; disk_alert=""; swap_alert=""
[[ -n "$press" ]] && { if (( press & 4 )); then pressure_alert=critical; else pressure_alert=normal; fi; }
[[ -n "$data_free_bytes" ]] && { if (( data_free_bytes < MEM_DISK_LOW_BYTES )); then disk_alert=low; else disk_alert=normal; fi; }
[[ -n "$swap_bytes" ]] && { if (( swap_bytes > MEM_SWAP_HIGH_BYTES )); then swap_alert=high; else swap_alert=normal; fi; }
edge pressure "$pressure_alert"; edge disk "$disk_alert"; edge swap "$swap_alert"

# Persist before attempting delivery: even a failed notifier must not page every tick.
if printf 'pressure\t%s\ndisk\t%s\nswap\t%s\n' "$previous[pressure]" "$previous[disk]" "$previous[swap]" \
     > "$ALERT_STATE.tmp" && mv "$ALERT_STATE.tmp" "$ALERT_STATE"; then
  if (( MEM_NOTIFY_ENABLE && ${#edges} )); then
    body="${(j:; :)edges}; available disk=${data_free_bytes:-?} bytes; swap=${swap_bytes:-?} bytes"
    # Complete setsid in the foreground dispatcher BEFORE it forks delivery and returns.
    # Backgrounding the dispatcher itself races launchd's process-group cleanup at exit.
    # flock is released by the OS on exit: no stale PID marker and no process signals.
    # A stuck notifier holds one lock/worker, never blocks sampling or accumulates workers.
    # Edges skipped while it is busy remain in EVENT history; delivery is best-effort.
    "${MEM_DETACH_COMMAND_OVERRIDE:-/usr/bin/perl}" -MPOSIX=setsid -MFcntl=:flock -e '
      my ($lockfile,$ts,@cmd)=@ARGV;
      setsid() != -1 or do { print "$ts NOTIFY-ERROR detach\n"; exit 1 };
      open(my $lock,">>",$lockfile) or do { print "$ts NOTIFY-ERROR lock-open\n"; exit 1 };
      flock($lock,LOCK_EX|LOCK_NB) or do { print "$ts NOTIFY-SKIPPED worker-busy\n"; exit 0 };
      my $pid=fork();
      defined($pid) or do { print "$ts NOTIFY-ERROR fork\n"; exit 1 };
      exit 0 if $pid;  # zsh waits only for this detached dispatcher, never delivery
      system {$cmd[0]} @cmd;
      my $rc=$?;
      if ($rc == -1) { print "$ts NOTIFY-ERROR spawn\n" }
      elsif ($rc & 127) { print "$ts NOTIFY-ERROR signal=".($rc & 127)."\n" }
      elsif ($rc >> 8) { print "$ts NOTIFY-ERROR exit=".($rc >> 8)."\n" }
    ' "$NOTIFY_LOCK" "$(ts)" "${MEM_NOTIFY_COMMAND_OVERRIDE:-/usr/bin/osascript}" \
      -e 'on run argv
        display notification (item 2 of argv) with title (item 1 of argv)
      end run' 'Memory watchdog' "$body" </dev/null >> "$LOG" 2>/dev/null \
      || print -- "$(ts) NOTIFY-ERROR dispatcher" >> "$LOG"
  fi
else
  print -- "$(ts) NOTIFY-ERROR state-write; no delivery attempted" >> "$LOG"
fi

# Test-only injection (unset in production / launchd): force a headroom state and/or feed
# synthetic process rows from a file, so the would-action ladder + kill-safety list can be
# verified deterministically without inducing real memory pressure. See goal/tools/test-decision.sh.
[[ -n "${MEM_FORCE_HEADROOM:-}" ]] && headroom="$MEM_FORCE_HEADROOM"
[[ -n "${MEM_TOPN_OVERRIDE:-}" ]] && MEM_TOP_N="$MEM_TOPN_OVERRIDE"   # test-only: raise display cap
# etime is here so that process IDENTITY, not just the PID, keys the history below. It costs
# nothing: same single `ps`, one extra column, placed before comm because comm may contain
# spaces and must stay last.
if [[ -n "${MEM_PS_SOURCE:-}" && -r "${MEM_PS_SOURCE}" ]]; then ps_cmd=(cat "$MEM_PS_SOURCE"); else ps_cmd=(ps -axo pid=,rss=,etime=,comm=); fi

NOW=$EPOCHSECONDS
: > "$STATE.tmp"
# Display-only: an unavailable signal reaches the log as `?`, never as a plausible number.
press_disp=${press:-?}; free_disp=${free_pct:-?}

# ---------- one ps -> one awk pass: classify, score, decide would-be action ----------
"${ps_cmd[@]}" | awk \
  -v NOW="$NOW" -v TS="$(ts)" -v STATEIN="$STATE" -v STATEOUT="$STATE.tmp" \
  -v HEADROOM="$headroom" -v PRESS="$press_disp" -v FREEPCT="$free_disp" \
  -v SWAP="${swap_used:-?}" -v SWAPD="$swap_delta" -v SWAPBYTES="${swap_bytes:-?}" \
  -v DATAFREE="${data_free_bytes:-?}" -v TOPN="$MEM_TOP_N" \
  -v W_SIZE="$MEM_W_SIZE" -v W_GROWTH="$MEM_W_GROWTH" -v W_PROFILE="$MEM_W_PROFILE" -v W_FG="$MEM_W_FOREGROUND" \
  -v GROWTH_REF="$MEM_GROWTH_REF_MBPS" -v R_WATCH="$MEM_RISK_WATCH" -v R_NOTIFY="$MEM_RISK_NOTIFY" -v R_KILL="$MEM_RISK_KILL" \
  -v GROWTH_MIN="$MEM_GROWTH_MIN_MBPS" -v SUSTAIN="$MEM_SUSTAIN_TICKS" -v RATIO_HARD="$MEM_RATIO_HARD" \
  -v SYS_TOL="$MEM_SYSTEM_TOL" -v SYS_BASE="$MEM_SYSTEM_BASE" \
  -v TRU_TOL="$MEM_TRUSTED_TOL" -v TRU_BASE="$MEM_TRUSTED_BASE" \
  -v DEV_TOL="$MEM_DEVRUNTIME_TOL" -v DEV_BASE="$MEM_DEVRUNTIME_BASE" \
  -v EPH_TOL="$MEM_EPHEMERAL_TOL" -v EPH_BASE="$MEM_EPHEMERAL_BASE" \
  -v UNK_TOL="$MEM_UNKNOWN_TOL" -v UNK_BASE="$MEM_UNKNOWN_BASE" \
  -v AUTOKILL="$MEM_AUTOKILL_PROFILES" -v PROTECT="$MEM_PROTECT" -v SESSION="$MEM_SESSION_PROTECT" \
  -v DEVRT="$MEM_DEVRUNTIME" -v EPHEM="$MEM_EPHEMERAL" '
  # Identity match for the kill-safety lists. NOT a substring of the full path: `codex` occurs
  # in /Users/me/.codex/tools/rg, `hidd` in /opt/homebrew/bin/hidden-daemon, `mds` in
  # .../src/mdsync/..., `logd` in /var/logd-shipper/agent, `Finder` in .../Finderesque/... —
  # every one of those was classified `system`, exempting a real runaway from the action ladder
  # and skewing observe-mode evidence. Codex flagged the SESSION case; PROTECT had it too, so
  # both consumers move to this. Three ways to match, all anchored:
  #   basename exactly              claude, kernel_task
  #   basename + separator suffix   com.apple.Virtualization.VirtualMachine, mdworker_shared
  #   an exact PATH SEGMENT         /opt/claude/bin/wrapper stays protected, while
  #                                 /Users/me/.codex/tools/rg does not (segment is ".codex")
  function ident_match(comm, b, list,   n,a,i,t,m,ns,sa,j) {
    ns = split(comm, sa, "/")
    n  = split(list, a, " ")
    for (i=1; i<=n; i++) {
      t = a[i]
      if (b == t) return 1
      m = t; gsub(/\./, "\\.", m)            # dots are literal, not regex wildcards
      if (b ~ ("^" m "([0-9._-]|$)")) return 1
      for (j=1; j<=ns; j++) if (sa[j] == t) return 1
    }
    return 0
  }
  function base_match(b, list,      n,a,i) { n=split(list,a," "); for(i=1;i<=n;i++) if(b==a[i] || b ~ ("^" a[i] "[0-9.]*$")) return 1; return 0 }
  function classify(comm, b) {
    if (ident_match(comm, b, PROTECT) || ident_match(comm, b, SESSION)) return "system"
    # trusted = signed .app bundle, OR an Apple system framework/service (these burst-allocate
    # legitimately — high tolerance, notify-first, never auto-kill). (D-008)
    if (index(comm, ".app/Contents/MacOS/") > 0) return "trusted"
    if (index(comm, "/System/Library/") > 0 || index(comm, "/System/Applications/") > 0) return "trusted"
    if (b ~ /^com\.apple\./) return "trusted"
    if (base_match(b, DEVRT)) return "devruntime"
    if (base_match(b, EPHEM)) return "ephemeral"
    return "unknown"
  }
  # ps etime -> seconds. Formats: SS, MM:SS, HH:MM:SS, DD-HH:MM:SS.
  function etime_sec(e,   n,p,t,d,s) {
    d=0; n=split(e,p,"-"); if(n==2){ d=p[1]+0; e=p[2] }
    n=split(e,t,":")
    if(n==3) s=t[1]*3600+t[2]*60+t[3]; else if(n==2) s=t[1]*60+t[2]; else s=e+0
    return d*86400+s
  }
  function tol_of(p)  { if(p=="system")return SYS_TOL; if(p=="trusted")return TRU_TOL; if(p=="devruntime")return DEV_TOL; if(p=="ephemeral")return EPH_TOL; return UNK_TOL }
  function base_of(p) { if(p=="system")return SYS_BASE;if(p=="trusted")return TRU_BASE;if(p=="devruntime")return DEV_BASE;if(p=="ephemeral")return EPH_BASE;return UNK_BASE }
  BEGIN {
    while ((getline line < STATEIN) > 0) {
      nf = split(line, f, "\t")
      # 6th field is the process START epoch. Rows written before it existed have nf<6 and get
      # start=-1, which never matches, so one tick of history is discarded on upgrade.
      if (nf >= 4) { prss[f[1]]=f[2]; fseen[f[1]]=f[3]; lseen[f[1]]=f[4]; streak[f[1]]=(nf>=5?f[5]:0); pstart[f[1]]=(nf>=6?f[6]:-1) }
    }
    close(STATEIN)
    nflag = 0; maxrisk = -1; maxcomm = "-"; nproc = 0
  }
  {
    pid=$1; rss_kb=$2; etime=$3; comm=$4; for(i=5;i<=NF;i++) comm=comm" "$i
    if (pid=="" || rss_kb=="") next
    nproc++
    # PID REUSE: a PID is only unique among LIVE processes. macOS recycles them, and keying
    # history on the PID alone lets a brand-new process inherit the dead RSS, last_seen and
    # growth streak — so it can be judged as having sustained growth on its very first tick.
    # Harmless while M0 only logs; it becomes a wrong kill the moment M1 acts on that evidence.
    # Identity is therefore (pid, start). start is derived from etime rather than read per
    # process, so the one-ps-per-tick budget is unchanged. A 5s tolerance absorbs the
    # one-second etime resolution and the drift between the ps snapshot and NOW; PIDs are not
    # reused within seconds on a machine with 100k of them to cycle through first.
    start = NOW - etime_sec(etime)
    if (pid in pstart) {
      d = start - pstart[pid]; if (d < 0) d = -d
      if (d > 5) { delete prss[pid]; delete fseen[pid]; delete lseen[pid]; delete streak[pid] }
      else start = pstart[pid]   # keep the first-seen value so it cannot drift tick by tick
    }
    # basename
    b=comm; m=comm; while ((p=index(m,"/"))>0) { m=substr(m,p+1) } b=m
    rss_mb = rss_kb/1024.0
    profile = classify(comm, b)
    tol = tol_of(profile); base = base_of(profile)
    ratio = rss_mb / base
    dt = (pid in lseen) ? (NOW - lseen[pid]) : 0
    pm = (pid in prss) ? prss[pid]/1024.0 : rss_mb
    growth = (dt>0 && rss_mb>pm) ? (rss_mb-pm)/dt : 0
    gnorm = growth / GROWTH_REF
    fg = (profile=="trusted") ? 1 : 0
    risk = W_SIZE*ratio + W_GROWTH*gnorm + W_PROFILE*(1.0/tol) - W_FG*fg
    if (risk < 0) risk = 0
    # sustained-growth streak (D-008): a single noisy burst tick never reaches an action.
    grewnow = (growth >= GROWTH_MIN) ? 1 : 0
    st = (grewnow ? (((pid in streak) ? streak[pid] : 0) + 1) : 0)
    # new state (6 fields: + grow streak + process start, the identity guard)
    f0 = (pid in fseen) ? fseen[pid] : NOW
    printf "%s\t%d\t%s\t%s\t%d\t%d\n", pid, rss_kb, f0, NOW, st, start > STATEOUT
    # would-be action (M0 never acts; this is the decision it WOULD make)
    # action-eligible only when growth has SUSTAINED, or RSS is already a runaway multiple of baseline
    eligible = (st >= SUSTAIN || ratio >= RATIO_HARD) ? 1 : 0
    autok = (index(" " AUTOKILL " ", " " profile " ") > 0) ? 1 : 0
    action = "ok"
    if (profile=="system") action = "protect"
    else {
      if (risk >= R_WATCH) action = "watch"
      if (eligible) {
        if (HEADROOM=="critical" && risk>=R_KILL && autok) action="WOULD-KILL"
        else if ((HEADROOM=="critical"||HEADROOM=="caution") && risk>=R_NOTIFY) action="WOULD-NOTIFY"
      }
    }
    if (risk > maxrisk) { maxrisk=risk; maxcomm=b }
    # collect flagged (anything reaching the watch floor, or any would-* action)
    if (risk >= R_WATCH || action=="WOULD-NOTIFY" || action=="WOULD-KILL") {
      nflag++
      fr[nflag]=risk
      ftext[nflag]=sprintf("%s %-12s %-26s pid=%-6s rss=%.0fM ratio=%.1f growth=%.1fMB/s streak=%d risk=%.2f profile=%s headroom=%s", \
        TS, action, b, pid, rss_mb, ratio, growth, st, risk, profile, HEADROOM)
    }
  }
  END {
    close(STATEOUT)
    printf "%s TICK headroom=%s press=%s free=%s%% swap=%sM(d%s) swap_bytes=%s data_free_bytes=%s procs=%d top=%s:%.2f\n", \
      TS, HEADROOM, PRESS, FREEPCT, SWAP, SWAPD, SWAPBYTES, DATAFREE, nproc, maxcomm, (maxrisk<0?0:maxrisk)
    # print up to TOPN flagged, highest risk first (BSD awk has no asort -> manual selection)
    printed=0
    while (printed < TOPN && printed < nflag) {
      bi=-1; bv=-1
      for (i=1;i<=nflag;i++) if (!done[i] && fr[i]>bv) { bv=fr[i]; bi=i }
      if (bi<0) break
      done[bi]=1; print ftext[bi]; printed++
    }
    if (nflag > TOPN) printf "%s   …and %d more at/above watch floor (capped at TOPN=%d)\n", TS, nflag-TOPN, TOPN
  }' >> "$LOG"

mv "$STATE.tmp" "$STATE"
