"""
Cairn tier-3 for the Python backend — served from EAGER recorded state.

The py-monitoring backend has no time-travel debugger; per-line locals were
recorded at capture time (backends/python/capture.py). Tier-3 "line-by-line"
and "state at instant N" are therefore INDEXES into that recording — same
response shape as the rr path, with the method labeled honestly.
"""

import json
import os

import layout

_CACHE = {}      # tid -> (mtime, {(fn, hit): record}, rows)


def _frames_py(tid):
    return os.path.join(layout.recording_dir(tid), "frames_py.jsonl")


def _eager_path(tid):
    return os.path.join(layout.recording_dir(tid), "loc_eager.jsonl")


def _load(tid, err):
    fp, ep = _frames_py(tid), _eager_path(tid)
    if not (os.path.exists(fp) and os.path.exists(ep)):
        raise err(f"no python recording for `{tid}`")
    mt = os.path.getmtime(ep)
    hit = _CACHE.get(tid)
    if hit and hit[0] == mt:
        return hit[1], hit[2]
    index = {}
    for line in open(ep):
        r = json.loads(line)
        index[(r["fn"], r["hit"])] = r
    rows = sorted((json.loads(l) for l in open(fp)), key=lambda r: r["step"])
    _CACHE[tid] = (mt, index, rows)
    return index, rows


def loc_for_frame(art, frame, instant=None, err=Exception):
    tid = art["scenario"]["id"]
    index, rows = _load(tid, err)
    if frame["step"] >= len(rows):
        raise err("frame step out of range")
    row = rows[frame["step"]]
    rec = index.get((row["fn"], row.get("hit_index", 1)))
    if rec is None:
        raise err(f"state not recorded for this activation "
                  f"(eager capture keeps the first activations per function; "
                  f"this was hit #{row.get('hit_index')})")

    lines = rec["lines"]
    # attach source text (the real file that ran)
    try:
        src = open(rec["file"], encoding="utf-8", errors="replace") \
            .read().splitlines()
        for ln in lines:
            n = ln["line"]
            ln["text"] = src[n - 1].rstrip() if 1 <= n <= len(src) else None
    except OSError:
        pass

    last = len(lines) - 1
    t = instant if (instant is not None and 0 <= instant <= last) else max(0, last - 1)
    reverse = {
        "supported": True,
        "method": "indexed recorded state (eager py-monitoring capture)",
        "reread_instant": t,
        "reread_line": lines[t]["line"] if lines else 0,
        "reread_locals": lines[t]["locals"] if lines else [],
        "matches_forward": True,    # it IS the forward recording, re-indexed
    }
    return {"function": row["fn"], "file": rec["file"],
            "entry_line": row["line"], "hit": row.get("hit_index", 1),
            "lines": lines, "reverse_step": reverse,
            "frame_id": frame["id"], "lane": frame["lane"],
            "line_count": len(lines), "source_path": rec["file"],
            "cached": True}
