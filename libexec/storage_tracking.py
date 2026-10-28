#!/usr/bin/env python3
"""Read-only storage metrics and existing Power Toolkit watchdog runner. No cleanup."""

import argparse
import collections
import ctypes
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from storage_native import Usage, allocation, attribute_writer
from storage_native import mask_cwd as mask_cwd

GIB = 1024**3


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def run(args, timeout=15):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, len(p.stderr.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        return -1, "", 1


def free_bytes(volume="/System/Volumes/Data"):
    rc, out, _ = run(["/bin/df", "-Pk", volume])
    try:
        return int(out.splitlines()[1].split()[3]) * 1024 if rc == 0 else None
    except (ValueError, IndexError):
        return None


def reasons(history, now, available, low, drop=2_000_000_000, window=600):
    if available is None:
        return ["unavailable"]
    result = []
    if available < low:
        result.append("low-space")
    recent = [b for t, b in history if 0 <= now - t <= window and b is not None]
    if recent and max(recent) - available > drop:
        result.append("fast-fall")
    return result


def capture_due(previous, current, entering, now, last, cooldown=300):
    return bool(current) and (
        entering
        or ("fast-fall" in current and "fast-fall" not in previous)
        or now - last >= cooldown
    )


def writer_deltas(before, after):
    old = {(r["pid"], r["start"]): r for r in before}
    rows = []
    for row in after:
        prev = old.get((row["pid"], row["start"]))
        if prev and row["written"] >= prev["written"]:
            delta = row["written"] - prev["written"]
            if delta:
                rows.append(
                    {
                        "pid": row["pid"],
                        "name": row["name"],
                        "start": row["start"],
                        "delta_bytes": delta,
                    }
                )
    return sorted(rows, key=lambda r: r["delta_bytes"], reverse=True)[:20]


def daily_changes(current, previous):
    rows = []
    for root, reading in current.items():
        old = previous.get(root, {})
        if reading.get("bytes") is not None and old.get("bytes") is not None:
            rows.append({"root": root, "delta_bytes": reading["bytes"] - old["bytes"]})
    return sorted(rows, key=lambda r: r["delta_bytes"], reverse=True)


def previous_day(directory, day):
    return max(
        (p for p in Path(directory).glob("????-??-??.json") if p.stem < day),
        default=None,
    )


def processes():
    if sys.platform != "darwin":
        return [], {"unavailable": "macOS libproc required"}
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return [], {"unavailable": "libproc load"}
    lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    lib.proc_name.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    rc, out, _ = run(["/bin/ps", "-axo", "pid="])
    if rc:
        return [], {"unavailable": "ps"}
    rows = []
    denied = 0
    for value in out.split():
        try:
            pid = int(value)
        except ValueError:
            continue
        u = Usage()
        if lib.proc_pid_rusage(pid, 2, ctypes.byref(u)) != 0:
            denied += 1
            continue
        b = ctypes.create_string_buffer(256)
        lib.proc_name(pid, b, 256)
        # Do not retain argv, cwd, paths, network identifiers or process environment.
        name = b.value.decode(errors="replace")
        name = re.sub(r"[^A-Za-z0-9_. -]", "_", name)[:80]
        rows.append({"pid": pid, "start": u.start, "written": u.written, "name": name})
    return rows, {"readable": len(rows), "unavailable": denied}


def native_metrics(volume="/System/Volumes/Data"):
    result = {}
    for label, args in [
        ("apfs", ["diskutil", "apfs", "list", "-plist"]),
        ("system_snapshots", ["diskutil", "apfs", "listSnapshots", "/", "-plist"]),
        ("data_snapshots", ["diskutil", "apfs", "listSnapshots", volume, "-plist"]),
    ]:
        try:
            p = subprocess.run(args, capture_output=True, timeout=15)
            if p.returncode:
                raise ValueError("native metric unavailable")
            d = plistlib.loads(p.stdout)
            if label == "apfs":
                result[label] = [
                    {
                        "capacity": c.get("CapacityCeiling"),
                        "free": c.get("CapacityFree"),
                        "volumes": [
                            {"roles": v.get("Roles"), "used": v.get("CapacityInUse")}
                            for v in c.get("Volumes", [])
                        ],
                    }
                    for c in d.get("Containers", [])
                ]
            else:
                result[label] = {
                    "count": len(d.get("Snapshots", [])),
                    "purgeable_count": sum(
                        bool(s.get("Purgeable")) for s in d.get("Snapshots", [])
                    ),
                }
        except (
            OSError,
            ValueError,
            plistlib.InvalidFileException,
            subprocess.TimeoutExpired,
        ):
            result[label] = {"unavailable": True}
    rc, out, _ = run(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used\s*=\s*([0-9.]+)([KMGT]?)", out) if rc == 0 else None
    result["swap_used_bytes"] = (
        int(float(m[1]) * 1024 ** (" KMGT".index(m[2]) if m[2] else 0)) if m else None
    )
    apfs = result.get("apfs")
    vm = (
        [
            v.get("used")
            for c in apfs
            for v in c["volumes"]
            if "VM" in (v.get("roles") or [])
        ]
        if isinstance(apfs, list)
        else []
    )
    result["vm_used_bytes"] = (
        sum(vm) if vm and all(isinstance(v, int) for v in vm) else None
    )
    # Foundation filesystem capacity estimates, no UI/control/notifications.
    js = (
        "ObjC.import('Foundation');var u=$.NSURL.fileURLWithPath("
        + json.dumps(volume)
        + ");var e=Ref();JSON.stringify(ObjC.deepUnwrap(u.resourceValuesForKeysError($([$.NSURLVolumeAvailableCapacityKey,$.NSURLVolumeAvailableCapacityForImportantUsageKey,$.NSURLVolumeAvailableCapacityForOpportunisticUsageKey]),e)));"
    )
    rc, out, _ = run(["/usr/bin/osascript", "-l", "JavaScript", "-e", js])
    try:
        result["capacity_estimates"] = json.loads(out) if rc == 0 else None
    except ValueError:
        result["capacity_estimates"] = None
    return result


def chrome_clone_count(xroot):
    try:
        base = Path(xroot) / "com.google.Chrome.code_sign_clone"
        if not base.exists():
            return 0 if Path(xroot).is_dir() else None
        return sum(
            1
            for p in base.iterdir()
            if p.name.startswith("code_sign_clone.")
            and p.is_dir()
            and not p.is_symlink()
        )
    except OSError:
        return None


def daily(
    directory,
    roots,
    incidents=None,
    timeout=180,
    volume="/System/Volumes/Data",
    discovery_unavailable=None,
    budget=1800,
):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = utc()
    day = stamp[:10]
    prior = previous_day(directory, day)
    try:
        previous = json.loads(prior.read_text()) if prior else {}
    except (OSError, ValueError):
        previous = {}
    readings = {}
    started = time.monotonic()
    for label, path in roots.items():
        remaining = budget - (time.monotonic() - started)
        readings[label] = (
            allocation(Path(path), timeout=min(timeout, remaining))
            if remaining > 0
            else {
                "bytes": None,
                "status": "unavailable-or-partial",
                "reason": "daily time budget",
            }
        )
    changes = daily_changes(readings, previous.get("roots", {}))
    native = native_metrics(volume)
    trends = {
        key: (native[key] - previous.get("native", {}).get(key))
        if isinstance(native.get(key), int)
        and isinstance(previous.get("native", {}).get(key), int)
        else None
        for key in ("swap_used_bytes", "vm_used_bytes")
    }
    if incidents is None:
        incidents = []
        for path in directory.parent.joinpath("monitor").glob("incident-*.json"):
            try:
                incident = json.loads(path.read_text())
                # Include the prior-day interval, not just incidents after today's midnight.
                if incident.get("utc", "")[:10] >= (prior.stem if prior else day):
                    incidents.append(incident)
            except (OSError, ValueError):
                continue
    report = {
        "utc": stamp,
        "available_bytes": free_bytes(volume),
        "native": native,
        "roots": readings,
        "prior_day": prior.stem if prior else None,
        "ranked_changes": changes,
        "trends": trends,
        "chrome_code_sign_clone_count": chrome_clone_count(roots["user-temp-X"])
        if "user-temp-X" in roots
        else None,
        "discovery_unavailable": discovery_unavailable or [],
        "incidents": sorted(incidents, key=lambda x: x.get("utc", "")),
        "limits": "Overlapping roots: do not sum. Private regular-file bytes exclude shared extents and directory metadata; not reclaim estimates. Unavailable is not zero.",
    }
    atomic(directory / (day + ".json"), report)

    def display(value):
        return "unavailable" if value is None else f"{value:,} bytes"

    lines = [
        "# Daily storage — " + day + " UTC",
        "",
        f"Available: {display(report['available_bytes'])}. Baseline: {report['prior_day'] or 'none'}.",
        "",
        "## Top growth (previous UTC date)",
        "",
    ]
    lines += [
        f"- {r['root']}: {r['delta_bytes']:+,} private bytes" for r in changes[:20]
    ] or ["No comparable prior-day readings."]
    lanes = [r for r in changes if r["root"].startswith("lane:")][:20]
    lines += ["", "## PWE lanes — top 20 by growth", ""]
    lines += [f"- {r['root']}: {r['delta_bytes']:+,} private bytes" for r in lanes] or [
        "No comparable lane baseline."
    ]
    lines += ["", "## Swap / VM trend", ""]
    for label, key in [("Swap", "swap_used_bytes"), ("VM volume", "vm_used_bytes")]:
        lines.append(
            f"- {label}: {display(native.get(key))}; change: {display(trends[key])}."
        )
    lines += ["", "## Incidents — Candidate writers", ""]
    for incident in report["incidents"]:
        lines.append(
            f"- {incident.get('utc', 'unavailable')}: {', '.join(incident.get('reasons', []))}"
        )
        for w in incident.get("candidate_writers", [])[:5]:
            ancestor = w.get("ancestor", {})
            lines.append(
                f"  - {w['name']} PID {w['pid']}: {w['delta_bytes']:,} I/O bytes; ancestor {ancestor.get('name', 'unavailable')} PID {ancestor.get('pid', 'unavailable')}, start {ancestor.get('start', 'unavailable')}, cwd {ancestor.get('cwd', 'unavailable')}."
            )
        if not incident.get("candidate_writers"):
            lines.append(
                "  - Candidate writers unavailable (baseline, exited or inaccessible)."
            )
    if not report["incidents"]:
        lines.append(
            "No retained incidents in the reporting interval; not evidence of complete coverage."
        )
    missing = sum(r.get("bytes") is None for r in readings.values())
    lines += [
        "",
        f"Coverage: {len(readings) - missing}/{len(readings)} complete roots; {missing} unavailable/partial.",
        "",
        f"Chrome code_sign_clone count: {report['chrome_code_sign_clone_count'] if report['chrome_code_sign_clone_count'] is not None else 'unavailable'}.",
        f"Root discovery gaps: {', '.join(report['discovery_unavailable']) or 'none reported'}.",
        "",
        report["limits"],
        "I/O/ancestry identifies candidates, not causal allocation. Retained incident slots may not cover the whole interval.",
    ]
    for name in (day + ".md", "latest.md"):
        atomic_text(directory / name, "\n".join(lines) + "\n")
    return report


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value)
    tmp.chmod(0o600)
    os.replace(tmp, path)


def settings(toolkit):
    keys = [
        "PT_LOG",
        "STORAGE_STATE",
        "STORAGE_INTERVAL_SEC",
        "STORAGE_FAST_FALL_BYTES",
        "STORAGE_WINDOW_SEC",
        "STORAGE_CAPTURE_COOLDOWN_SEC",
        "STORAGE_INCIDENT_SLOTS",
        "STORAGE_SCAN_TIMEOUT_SEC",
        "STORAGE_DAILY_BUDGET_SEC",
        "STORAGE_DAILY_HOUR",
        "STORAGE_DAILY_MINUTE",
        "STORAGE_DEVELOPMENT",
        "STORAGE_PWE_LOCAL",
        "STORAGE_USER_TEMP",
        "STORAGE_USER_CACHE",
        "STORAGE_ATTRIBUTION_TIMEOUT_SEC",
        "STORAGE_ENABLE",
        "MEM_DATA_VOLUME",
        "MEM_WATCHDOG_LABEL",
        "STORAGE_DAILY_LABEL",
    ]
    script = 'PT_ROOT="$1"; source "$PT_ROOT/config.sh" || exit; '
    script += (
        "for key in "
        + " ".join(keys)
        + '; do printf "%s\\0%s\\0" "$key" "${(P)key}"; done; '
    )
    script += 'for key in ${(k)STORAGE_ROOTS}; do printf "%s\\0%s\\0" "root:$key" "$STORAGE_ROOTS[$key]"; done'
    rc, out, _ = run(["/bin/zsh", "-f", "-c", script, "storage-tracking", str(toolkit)])
    values = out.split("\0")
    if rc or not out.endswith("\0"):
        raise RuntimeError("storage config unavailable")
    result = dict(zip(values[0:-1:2], values[1:-1:2]))
    for key in (
        "STORAGE_INTERVAL_SEC",
        "STORAGE_WINDOW_SEC",
        "STORAGE_INCIDENT_SLOTS",
        "STORAGE_SCAN_TIMEOUT_SEC",
        "STORAGE_ATTRIBUTION_TIMEOUT_SEC",
    ):
        if int(result[key]) < 1:
            raise RuntimeError("storage positive setting required")
    return result


def discover_roots(config):
    # Only safe root labels leave discovery; individual filenames never leave walks.
    roots = {k[5:]: v for k, v in config.items() if k.startswith("root:")}
    missing = []

    def label(value):
        if re.search(
            r"private|writing|secret|wispr|media|photos|pictures|movies|music",
            value,
            re.I,
        ):
            return "redacted-" + hashlib.sha256(value.encode()).hexdigest()[:16]
        return re.sub(r"[^A-Za-z0-9_.:/-]", "_", value)[:240]

    for category, key in [("T", "STORAGE_USER_TEMP"), ("C", "STORAGE_USER_CACHE")]:
        path = config.get(key, "")
        if path:
            roots["user-temp-" + category] = path
        else:
            missing.append("user-temp-" + category)
    temp = config.get("STORAGE_USER_TEMP", "")
    if temp:
        roots["user-temp-X"] = str(Path(temp.rstrip("/")).parent / "X")
        if not config.get("STORAGE_USER_CACHE"):
            roots["user-temp-C"] = str(Path(temp.rstrip("/")).parent / "C")
            missing = [x for x in missing if x != "user-temp-C"]
    # Claude temp roots, no individual scratch names exported.
    for i, path in enumerate(sorted(Path("/private/tmp").glob("claude-*"))):
        if path.is_dir() and not path.is_symlink():
            roots[
                "claude-temp:" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
            ] = str(path)
    # PWE local/<campaign>/<lane>; direct lane roots remain visible as campaign roots.
    local = Path(config["STORAGE_PWE_LOCAL"])
    try:
        for campaign in sorted(local.iterdir()):
            if not campaign.is_dir() or campaign.is_symlink():
                continue
            children = [
                p for p in campaign.iterdir() if p.is_dir() and not p.is_symlink()
            ]
            for lane in children or [campaign]:
                roots["lane:" + label(str(lane.relative_to(local)))] = str(lane)
    except OSError:
        missing.append("lane-discovery")
    development = Path(config["STORAGE_DEVELOPMENT"])
    start = time.monotonic()
    excluded = {"node_modules", ".git", ".venv", "venv", "local", ".cache", "vendor"}

    def error(_):
        missing.append("worktree-discovery")

    for base, dirs, _ in os.walk(development, followlinks=False, onerror=error):
        if time.monotonic() - start > int(config["STORAGE_SCAN_TIMEOUT_SEC"]):
            missing.append("worktree-discovery-timeout")
            break
        keep = []
        for d in dirs:
            path = Path(base) / d
            if path.is_symlink() or d in excluded:
                continue
            if "-wt-" in d:
                roots["worktree:" + label(str(path.relative_to(development)))] = str(
                    path
                )
            else:
                keep.append(d)
        dirs[:] = keep
    return roots, sorted(set(missing))


def generate_plists(directory, toolkit, config):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = {
        "ProcessType": "Background",
        "StandardOutPath": "/dev/null",
    }
    if int(config["STORAGE_ENABLE"]):
        mem = {
            **base,
            "Label": config["MEM_WATCHDOG_LABEL"],
            "ProgramArguments": [str(toolkit / "bin/storage-tracking"), "monitor"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ExitTimeOut": 30,
        }
    else:
        mem = {
            **base,
            "Label": config["MEM_WATCHDOG_LABEL"],
            "ProgramArguments": [str(toolkit / "libexec/mem-watchdog.sh")],
            "RunAtLoad": True,
            "StartInterval": config_values(toolkit)["memory_interval"],
        }
    daily = {
        **base,
        "Label": config["STORAGE_DAILY_LABEL"],
        "ProgramArguments": [str(toolkit / "bin/storage-tracking"), "daily"],
        "RunAtLoad": False,
        "StartCalendarInterval": {
            "Hour": int(config["STORAGE_DAILY_HOUR"]),
            "Minute": int(config["STORAGE_DAILY_MINUTE"]),
        },
    }
    if not int(config["STORAGE_ENABLE"]):
        daily.pop("StartCalendarInterval")
        daily["Disabled"] = True
    # Validate/stage the complete set before changing a persistent member.
    labels = (mem["Label"], daily["Label"])
    if len(set(labels)) != 2 or any(
        not re.fullmatch(r"[A-Za-z0-9._-]+", name) for name in labels
    ):
        raise ValueError("distinct storage job labels required")
    # Retain private recovery bundles, including after failed restoration. A
    # failed rollback must never lose the only prior copy during temp cleanup.
    stage = Path(tempfile.mkdtemp(prefix=".storage-generate-", dir=directory))
    targets = []
    for job in (mem, daily):
        job["StandardErrorPath"] = str(Path(config["PT_LOG"]) / (job["Label"] + ".err.log"))
        name = job["Label"] + ".plist"
        target = directory / name
        staged = stage / name
        atomic_text(staged, plistlib.dumps(job).decode())
        plistlib.loads(staged.read_bytes())
        backup = stage / (name + ".prior")
        if target.exists():
            shutil.copy2(target, backup)
        targets.append((target, staged, backup))
    published = []
    try:
        for target, staged, backup in targets:
            os.replace(staged, target)
            published.append((target, backup))
    except OSError:
        incomplete = False
        for target, backup in reversed(published):
            try:
                if backup.exists():
                    os.replace(backup, target)
                else:
                    # Restore absence, retaining rejected config, never delete.
                    os.replace(target, stage / (target.name + ".rejected"))
            except OSError:
                incomplete = True
        print(
            "storage generation rollback " + ("incomplete" if incomplete else "restored")
            + "; recovery bundle: " + str(stage), file=sys.stderr,
        )
        raise
    return mem, daily


def config_values(toolkit):
    # Same config/profile evaluation as existing watchdog; do not parse shell arithmetic ourselves.
    script = 'PT_ROOT="$1"; source "$PT_ROOT/config.sh"; print -r -- "$MEM_DISK_LOW_BYTES $MEM_WATCHDOG_INTERVAL_SEC"'
    rc, out, _ = run(["/bin/zsh", "-f", "-c", script, "storage-tracking", str(toolkit)])
    values = out.split()
    if rc or len(values) != 2 or not all(x.isdigit() for x in values):
        raise RuntimeError("Power Toolkit config unavailable: threshold/interval")
    if int(values[1]) < 1:
        raise RuntimeError("Power Toolkit interval must be positive")
    return {"low": int(values[0]), "memory_interval": int(values[1])}


class Recorder:
    def __init__(
        self, directory, low, config=None, ancestor_reader=None, cwd_reader=None
    ):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.low = low
        self.config = config or {}
        self.reader = ancestor_reader
        self.cwd_reader = cwd_reader
        self.interval = int(self.config.get("STORAGE_INTERVAL_SEC", 5))
        self.window = int(self.config.get("STORAGE_WINDOW_SEC", 600))
        self.history = collections.deque(maxlen=self.window // self.interval + 1)
        self.ring = collections.deque(maxlen=25)
        self.previous = []
        self.last_capture = -1e30
        self.last_low = None
        self.previous_incident = []
        self.slots = int(self.config.get("STORAGE_INCIDENT_SLOTS", 40))
        self.sequence = 0
        try:
            self.sequence = int(
                json.loads((self.directory / "status.json").read_text())[
                    "incident_sequence"
                ]
            )
        except (OSError, ValueError, KeyError):
            pass

    def record(self, tick, free, p, coverage):
        deltas = writer_deltas(self.previous, p)
        self.previous = p
        self.history.append((tick, free))
        why = reasons(
            self.history,
            tick,
            free,
            self.low,
            int(self.config.get("STORAGE_FAST_FALL_BYTES", 2000000000)),
            self.window,
        )
        sample = {
            "utc": utc(),
            "free_bytes": free,
            "top_writer_deltas": deltas,
            "process_coverage": coverage,
            "reasons": why,
        }
        self.ring.append(sample)
        atomic(self.directory / "ring.json", list(self.ring))
        incident = [x for x in why if x in ("low-space", "fast-fall")]
        entering = free is not None and free < self.low and self.last_low is not True
        captured = capture_due(
            self.previous_incident,
            incident,
            entering,
            tick,
            self.last_capture,
            int(self.config.get("STORAGE_CAPTURE_COOLDOWN_SEC", 300)),
        )
        if captured:
            # Rank interval totals from retained samples. Only attribute at incident time.
            writers = {}
            for row in self.ring:
                for w in row["top_writer_deltas"]:
                    key = (w["pid"], w["start"])
                    if key not in writers:
                        writers[key] = {**w, "delta_bytes": 0}
                    writers[key]["delta_bytes"] += w["delta_bytes"]
            candidates = sorted(
                writers.values(), key=lambda w: w["delta_bytes"], reverse=True
            )[:20]
            attribution_start = time.monotonic()
            for w in candidates:
                if time.monotonic() - attribution_start > int(
                    self.config.get("STORAGE_ATTRIBUTION_TIMEOUT_SEC", 8)
                ):
                    w["ancestor"] = {
                        "status": "unavailable",
                        "reason": "incident attribution time budget",
                    }
                    continue
                kwargs = {
                    "deadline": attribution_start
                    + int(self.config.get("STORAGE_ATTRIBUTION_TIMEOUT_SEC", 8))
                }
                if self.reader is not None:
                    kwargs["reader"] = self.reader
                if self.cwd_reader is not None:
                    kwargs["cwd_reader"] = self.cwd_reader
                w["ancestor"] = attribute_writer(w, **kwargs)
            atomic(
                self.directory / f"incident-{self.sequence % self.slots:02d}.json",
                {
                    "utc": utc(),
                    "reasons": incident,
                    "threshold": self.low,
                    "samples": list(self.ring),
                    "candidate_writers": candidates,
                    "native": native_metrics(
                        self.config.get("MEM_DATA_VOLUME", "/System/Volumes/Data")
                    ),
                    "attribution_limit": "I/O and ancestry are candidates, not exclusive per-file allocation",
                },
            )
            self.sequence += 1
            self.last_capture = tick
        if free is not None:
            self.last_low = free < self.low
            self.previous_incident = incident
        return sample, captured


def monitor(directory, toolkit, once=False, skip_memory=False):
    # Exclusion needs no writable state: ENOSPC in telemetry must not gate the
    # guardian. One monitor per permanent toolkit, even with different outputs.
    with (toolkit / "libexec/storage_tracking.py").open("rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another storage runner owns this toolkit")
        config = config_values(toolkit)
        cfg = None
        directory = Path(directory) if directory is not None else None
        interval = 1.0  # Retry unavailable telemetry without reading storage settings here.
        recorder = None
        memory = None
        last_memory = -1e30
        next_sample = -1e30
        worker = None
        alarm = threading.Event()
        result = {}
        guardian = {}

        def telemetry():
            nonlocal recorder, cfg, directory, interval
            try:
                if cfg is None:
                    cfg = settings(toolkit)
                    interval = int(cfg["STORAGE_INTERVAL_SEC"])
                if directory is None:
                    directory = Path(cfg["STORAGE_STATE"]) / "monitor"
                if recorder is None:
                    recorder = Recorder(directory, config["low"], cfg)
                tick = time.monotonic()
                free = free_bytes(cfg["MEM_DATA_VOLUME"])
                processes_read, coverage = processes()
                sample, captured = recorder.record(tick, free, processes_read, coverage)
                if captured and "fast-fall" in sample["reasons"]:
                    alarm.set()
                result["sample"] = sample
                atomic(
                    directory / "status.json",
                    {
                        "utc": utc(),
                        "status": "degraded"
                        if free is None or not processes_read
                        or bool(coverage.get("unavailable"))
                        or guardian.get("dispatch_error")
                        or (memory is not None and memory.poll() not in (None, 0))
                        else "observing",
                        "free_bytes": free,
                        "reasons": sample["reasons"],
                        "process_coverage": coverage,
                        "memory_watchdog_rc": memory.poll() if memory else None,
                        "memory_watchdog_elapsed": time.monotonic() - last_memory if memory else None,
                        "guardian": dict(guardian),
                        "incident_sequence": recorder.sequence,
                    },
                )
            except Exception as error:
                # No error text, paths, argv or environment are retained.
                failure = {
                    "utc": utc(), "status": "telemetry-unavailable",
                    "phase": "monitor", "error_type": type(error).__name__,
                    "errno": getattr(error, "errno", None),
                }
                print(json.dumps(failure), file=sys.stderr)
                try:
                    if directory is not None:
                        atomic(directory / "last-error.json", failure)
                except OSError:
                    pass

        while True:
            tick = time.monotonic()
            fast = alarm.is_set()
            if (
                not skip_memory
                and ((fast and not guardian.get("dispatch_error")) or tick - last_memory >= config["memory_interval"])
                and (memory is None or memory.poll() is not None)
            ):
                env = os.environ.copy()
                env["MEM_STORAGE_FAST_FALL"] = "1" if fast else "0"
                # Launch errors retry on the normal cadence, separately from
                # telemetry; existing guardian thresholds/notifier are unchanged.
                last_memory = tick
                try:
                    memory = subprocess.Popen(
                        ["/bin/zsh", "-f", str(toolkit / "libexec/mem-watchdog.sh")],
                        env=env, stdout=subprocess.DEVNULL,
                    )
                    guardian.update(dispatch_utc=utc(), dispatch_error=None)
                    alarm.clear()
                except OSError as error:
                    guardian.update(dispatch_error=type(error).__name__)
                    print("memory guardian dispatch unavailable: " + type(error).__name__, file=sys.stderr)
            # A single daemon worker owns all state writes, scans and native
            # capture. A blocked call cannot grow a queue or delay this scheduler.
            if (worker is None or not worker.is_alive()) and tick >= next_sample:
                worker = threading.Thread(target=telemetry, daemon=True)
                worker.start()
                next_sample = tick + interval
            if once:
                worker.join()  # diagnostic one-shot explicitly waits for its sample
                return result.get("sample")
            remaining = config["memory_interval"] - (time.monotonic() - last_memory)
            time.sleep(min(1.0, interval, remaining) if remaining > 0 else min(1.0, interval))


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "mode", choices=["daily", "monitor", "process-probe", "generate-plists"]
    )
    p.add_argument("--output", type=Path)
    p.add_argument("--toolkit", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--once", action="store_true")
    p.add_argument("--skip-memory-watchdog", action="store_true")
    args = p.parse_args()
    output = args.output
    try:
        if args.mode == "monitor":
            # Storage settings and the default state path belong to telemetry;
            # only the guardian's config_values() may gate monitor startup.
            monitor(output, args.toolkit, args.once, args.skip_memory_watchdog)
            return
        cfg = settings(args.toolkit)
        output = output or Path(cfg["STORAGE_STATE"]) / (
            "daily" if args.mode == "daily" else "monitor"
        )
        if args.mode == "generate-plists":
            if args.output is None:
                p.error("generate-plists requires --output")
            generate_plists(output, args.toolkit, cfg)
        elif args.mode == "daily":
            if not int(cfg["STORAGE_ENABLE"]):
                return
            output.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (output / "daily.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                roots, missing = discover_roots(cfg)
                report = daily(
                    output,
                    roots,
                    timeout=int(cfg["STORAGE_SCAN_TIMEOUT_SEC"]),
                    volume=cfg["MEM_DATA_VOLUME"],
                    discovery_unavailable=missing,
                    budget=int(cfg["STORAGE_DAILY_BUDGET_SEC"]),
                )
                print(
                    json.dumps(
                        {
                            "utc": report["utc"],
                            "roots": len(report["roots"]),
                            "discovery_unavailable": missing,
                        }
                    )
                )
        elif args.mode == "process-probe":
            rows, coverage = processes()
            atomic(output, {"utc": utc(), "coverage": coverage, "rows": rows})
            print(json.dumps(coverage))
    except Exception as error:
        if output is None:
            raise
        target = output.parent if args.mode == "process-probe" else output
        try:
            atomic(
                target / "last-error.json",
                {
                    "utc": utc(),
                    "status": "down",
                    "phase": args.mode,
                    "error_type": type(error).__name__,
                    "errno": getattr(error, "errno", None),
                },
            )
        except OSError:
            pass
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("storage tracking stopped: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
