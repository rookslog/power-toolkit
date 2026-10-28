"""macOS metadata only. Never reads argv, environment, file data or raw logs."""

import datetime
import ctypes
from functools import lru_cache
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import time


# Apple SDK sys/resource.h rusage_info_v2, disk bytes fields; identity includes start time.
class Usage(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (n, ctypes.c_uint64)
        for n in (
            "user",
            "system",
            "pkg",
            "interrupt",
            "pageins",
            "wired",
            "resident",
            "footprint",
            "start",
            "exit",
            "child_user",
            "child_system",
            "child_pkg",
            "child_interrupt",
            "child_pageins",
            "child_elapsed",
            "read",
            "written",
        )
    ]


class Attrlist(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_ushort)] + [
        (n, ctypes.c_uint32)
        for n in ("commonattr", "volattr", "dirattr", "fileattr", "forkattr")
    ]


@lru_cache(maxsize=1)
def attr_library():
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    lib.getattrlist.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(Attrlist),
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_ulong,
    ]
    return lib


def private_size(path):
    """ATTR_CMNEXT_PRIVATESIZE; FSOPT_ATTR_CMN_EXTENDED | FSOPT_NOFOLLOW."""
    if sys.platform != "darwin":
        return None
    try:
        lib = attr_library()
        attrs = Attrlist(5, 0, 0, 0, 0, 0, 0x8)
        buf = ctypes.create_string_buffer(32)
        if lib.getattrlist(os.fsencode(path), ctypes.byref(attrs), buf, len(buf), 0x21):
            return None
        if struct.unpack_from("=I", buf.raw)[0] != 12:
            return None
        return struct.unpack_from("=Q", buf.raw, 4)[0]
    except (OSError, ValueError):
        return None


def allocation(path, timeout=180, private_reader=private_size):
    """Serial, same-device, hardlink-deduplicated regular-file private bytes.

    Partial reads have bytes=None plus a non-authoritative observed subtotal.
    No individual paths or error messages leave this function.
    """
    start = time.monotonic()
    counts = {"files": 0, "errors": 0, "private_unavailable": 0, "allocated_bytes": 0}
    total = 0
    timed_out = False
    seen = set()
    try:
        root = Path(path).expanduser()
        st = root.lstat()
        if not stat.S_ISDIR(st.st_mode):
            raise OSError("not a directory")
        device = st.st_dev

        def error(_):
            counts["errors"] += 1

        for base, dirs, files in os.walk(root, followlinks=False, onerror=error):
            if time.monotonic() - start >= timeout:
                timed_out = True
                break
            keep = []
            for d in dirs:
                try:
                    sd = (Path(base) / d).lstat()
                    if stat.S_ISDIR(sd.st_mode) and sd.st_dev == device:
                        keep.append(d)
                except OSError:
                    counts["errors"] += 1
            dirs[:] = keep
            for name in files:
                if time.monotonic() - start >= timeout:
                    timed_out = True
                    break
                p = Path(base) / name
                try:
                    st = p.lstat()
                    if not stat.S_ISREG(st.st_mode) or st.st_dev != device:
                        continue
                    key = (st.st_dev, st.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                    counts["files"] += 1
                    counts["allocated_bytes"] += st.st_blocks * 512
                    value = private_reader(p)
                    if value is None:
                        counts["private_unavailable"] += 1
                    else:
                        total += value
                except OSError:
                    counts["errors"] += 1
            if timed_out:
                break
    except OSError:
        counts["errors"] += 1
    complete = not (timed_out or counts["errors"] or counts["private_unavailable"])
    return {
        "bytes": total if complete else None,
        "observed_private_bytes": total,
        "status": "ok" if complete else "unavailable-or-partial",
        "method": "ATTR_CMNEXT_PRIVATESIZE, regular files only",
        "timeout": timed_out,
        **counts,
    }


def mask_cwd(value):
    if not value or not os.path.isabs(value):
        return "unavailable"
    # Cwd is authorized metadata, but private-writing/media names must not escape.
    if any(
        re.search(
            r"private|writing|secret|wispr|media|photos|pictures|movies|music|\.env",
            part,
            re.I,
        )
        for i, part in enumerate(Path(value).parts)
        if not (i == 1 and part == "private")
    ):
        return "unavailable"
    home = str(Path.home())
    masked = (
        "~" + value[len(home) :]
        if value == home or value.startswith(home + "/")
        else value
    )
    return re.sub(r"[\x00-\x1f\x7f|`]", "_", masked)[:1024]


def process_info(pid):
    """Read only PID/PPID, executable name/path and native start identity."""
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        # SDK rusage_info_v2: start is ri_proc_start_abstime (mach absolute ticks).
        u = Usage()
        if lib.proc_pid_rusage(pid, 2, ctypes.byref(u)):
            return None
        p = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "ppid=,lstart="],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "LC_ALL": "C"},
        )
        fields = p.stdout.split()
        if p.returncode or len(fields) != 6 or not fields[0].isdigit():
            return None
        name = ctypes.create_string_buffer(256)
        lib.proc_name(pid, name, len(name))
        executable = ctypes.create_string_buffer(4096)
        lib.proc_pidpath(pid, executable, len(executable))
        check = Usage()
        if lib.proc_pid_rusage(pid, 2, ctypes.byref(check)) or check.start != u.start:
            return None
        return {
            "pid": pid,
            "ppid": int(fields[0]),
            "started_at": datetime.datetime.strptime(
                " ".join(fields[1:]), "%a %b %d %H:%M:%S %Y"
            )
            .astimezone()
            .astimezone(datetime.timezone.utc)
            .isoformat(),
            "start": u.start,
            "name": re.sub(
                r"[^A-Za-z0-9_. -]", "_", name.value.decode(errors="replace")
            )[:80],
            "app": b".app/Contents/" in executable.value,
        }
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def process_cwd(pid):
    # lsof restricted to cwd, field output; never full command lines or open files.
    try:
        p = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if p.returncode:
            return None
        return next(
            (line[1:] for line in p.stdout.splitlines() if line.startswith("n/")), None
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def attribute_writer(
    writer, reader=process_info, cwd_reader=process_cwd, deadline=None
):
    unavailable = {
        "status": "unavailable",
        "reason": "exited, changed identity, inaccessible or no eligible ancestor",
    }
    row = reader(writer["pid"])
    if not row or row["start"] != writer["start"]:
        return unavailable
    seen = set()
    chain = []
    for _ in range(32):
        if deadline is not None and time.monotonic() >= deadline:
            return {
                "status": "unavailable",
                "reason": "incident attribution time budget",
            }
        if not row or row["pid"] in seen:
            return unavailable
        seen.add(row["pid"])
        chain.append(dict(row))
        if row["name"].lower() in ("claude", "codex", "node") or row.get("app"):
            cwd = cwd_reader(row["pid"])
            # Re-read every identity and parent relationship after cwd access.
            # PID/start alone on the endpoints cannot bind intermediate edges.
            for saved in reversed(chain):
                if deadline is not None and time.monotonic() >= deadline:
                    return unavailable
                check = reader(saved["pid"])
                if not check or any(
                    check.get(key) != saved.get(key) for key in ("pid", "start", "ppid")
                ):
                    return unavailable
            return {
                "status": "candidate",
                "name": row["name"],
                "pid": row["pid"],
                "start": row["start"],
                "started_at": row.get("started_at", "unavailable"),
                "start_unit": "mach_absolute_ticks",
                "cwd": mask_cwd(cwd),
                "limit": "ancestry/cwd candidate, not file ownership",
            }
        if row.get("ppid", 0) <= 1:
            break
        parent = reader(row["ppid"])
        # A parent must exist before its child; a younger process is a reused PID.
        if not parent or parent["pid"] != row["ppid"] or parent["start"] > row["start"]:
            return unavailable
        row = parent
    return unavailable
