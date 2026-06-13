"""
XRay record-time structural capture (the fast path).

The breakpoint-replay extractor stops at every function over a remote gdb
protocol — O(stops x round-trip), prohibitive for big cross-crate traces. XRay
instead instruments the binary at COMPILE time; during the native-speed
`rr record` run it logs every function entry/exit to a buffer. We get the exact
call tree for free, then split the FrameStream into:

  structure  (fn / file / line / depth / call-nesting / counts)  <- XRay log
  arms       (.returns: Ok/Err/Some/None per function)           <- bounded gdb
             pass over the SAME rr recording, once per function (enough for the
             cross-trace flip detection the diff/bench rely on)

One rr recording of the XRay-instrumented binary yields both, plus the
replayable recording for tier-3. Proven end-to-end: the XRay-derived structure
matches the gdb extractor field-for-field; arms read back via the warm session.

Build recipe (in the mining clone, never p2claw):
  rustc/cargo +nightly  -Z instrument-xray=always  -C relocation-model=static
    -C link-arg=<map_dump.o>  -C link-arg=-no-pie
    -C link-arg=-Wl,--whole-archive <libclang_rt.xray*.a> -Wl,--no-whole-archive
    -lstdc++ -lpthread -ldl
  run:  XRAY_OPTIONS="patch_premain=true xray_mode=xray-basic xray_logfile_base=..."
        XRAY_BASIC_OPTIONS="func_duration_threshold_us=0" CAIRN_XRAY_MAP=<map>
"""

import json
import os
import re
import struct
import subprocess

# basic-mode record: [pad][pad][0x04][type: 0=enter 1=exit 2=tail][funcid u32]
# [tsc u64][tid u32][pid u32][pad] = 32 bytes, after a 32-byte file header.
_REC = 32
ENTER, EXIT, TAIL = 0, 1, 2


def _resolve_map(map_path, binary, crate_mark):
    """func_id -> (fn, file, line) for USER functions only. With all crates
    instrumented the map has ~40k entries; resolving every one via addr2line is
    slow, so we filter FIRST by the crate's symbol (the v0-mangled name carries
    the crate, e.g. `p2claw_box_agent`) using one nm pass, then addr2line only
    the ~hundreds of user functions for file:line."""
    ids = {int(l.split()[0]): int(l.split()[1], 16) for l in open(map_path)}
    addr_of = {a: i for i, a in ids.items() if a}
    # one nm pass: address -> mangled symbol; keep only this crate's
    nm = subprocess.run(["nm", binary], capture_output=True, text=True).stdout
    want = {}                                   # addr -> func_id, user only
    for line in nm.splitlines():
        p = line.split()
        if len(p) >= 3 and p[1] in "Tt" and crate_mark in p[2]:
            a = int(p[0], 16)
            if a in addr_of:
                want[a] = addr_of[a]
    if not want:
        return {}
    addrs = sorted(want)
    # batched demangling addr2line on just the user functions
    r = subprocess.run(["addr2line", "-f", "-C", "-e", binary]
                       + [hex(a) for a in addrs], capture_output=True, text=True
                       ).stdout.split("\n")
    out = {}
    for k, a in enumerate(addrs):
        fn = r[2 * k] if 2 * k < len(r) else "?"
        loc = r[2 * k + 1] if 2 * k + 1 < len(r) else "?"
        file, _, line = loc.rpartition(":")
        # FILE fence (the real user-code filter, matching gdb): defined under
        # the crate's own src/, not a generic instantiated from elsewhere.
        if file and fn not in ("??", "") and FILE_MARK in file:
            out[want[a]] = {"fn": fn, "file": file,
                            "line": int(line) if line.isdigit() else 0}
    return out


FILE_MARK = "/src/"   # set per project; box-agent paths are crates/<c>/src/...


def _events(log_path):
    data = open(log_path, "rb").read()[_REC:]      # skip header
    for k in range(0, len(data) - _REC + 1, _REC):
        r = data[k:k + _REC]
        yield r[3], struct.unpack("<I", r[4:8])[0]  # (type, func_id)


def structure(log_path, map_path, binary, crate_mark, hit_cap=40):
    """Reconstruct the FrameStream from the XRay trace, fenced to the user
    crate. `crate_mark` is the crate's mangled-symbol marker (e.g.
    'p2claw_box_agent'). Returns (frames, counts) — the exact schema the
    breakpoint extractor emits, at native speed."""
    umap = _resolve_map(map_path, binary, crate_mark)
    stack, frames, counts, step = [], [], {}, 0
    for typ, fid in _events(log_path):
        if typ == ENTER:
            stack.append(fid)
            m = umap.get(fid)
            if not m:
                continue
            depth = sum(1 for s in stack if s in umap)   # user-relative nesting
            c = counts.get(m["fn"], 0) + 1
            counts[m["fn"]] = c
            if c <= hit_cap:
                frames.append({"step": step, "fn": m["fn"], "file": m["file"],
                               "line": m["line"], "depth": depth, "args": [],
                               "hit_index": c})
                step += 1
        elif typ in (EXIT, TAIL):
            if stack:
                stack.pop()
    return frames, counts


# arm derivation handles BOTH the printer-rendered form (`Ok(Claims{...})`) and
# gdb's raw enum form (`{0, Ok = {...}, Err = {...}}` -> variant at discriminant).
_CLEAN = re.compile(r"\s*(Ok|Err|Some|None|true|false)\b")
_RAW = re.compile(r"\{\s*(\d+)\s*,\s*(.+)\}\s*$")


def arm_of_value(v):
    m = _CLEAN.match(v or "")
    if m:
        a = m.group(1).lower()
        return {"true": "true", "false": "false"}.get(a, a)
    m = _RAW.match(v or "")
    if m:
        disc = int(m.group(1))
        variants = re.findall(r"(\w+)\s*=", m.group(2))
        if disc < len(variants):
            return variants[disc].lower()
    return None
