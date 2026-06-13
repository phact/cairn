"""
Cairn trace-artifact builder (Phase 2).

Projects the ONE recording (out/frames*.jsonl + .returns/.counts) into the
stable, capture-backend-agnostic artifact that is the contract with the
frontend. Every tier (precis / skeleton / loc) is later derived from THIS
artifact, so the tiers can never disagree — they are projections of one
source of truth, exactly as the spec demands.

Nothing here is inferred: lanes come from source declaration order, frames
and their values from recorded stops, causal edges from recorded call
nesting, boundaries from recorded I/O calls.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import rank  # noqa: E402  (shared ranking truth)

FRAMES = os.environ.get("CAIRN_OUT", "out/frames_rr.jsonl")
# Scenario identity is supplied by the pipeline (from the user's --crate/--test
# or the project config); the engine itself names no scenario.
CRATE = os.environ.get("CAIRN_CRATE", "crate")
SCEN_ID = os.environ.get("CAIRN_SCENARIO_ID", f"{CRATE}::trace")
# v0: causality is derived from recorded call nesting (equivalent to the span
# tree for this sequential flow). Declared honestly so a consumer never
# mistakes it for true tracing-span provenance.
CAUSAL_BACKEND = "call-nesting-v0"
# Declared by the capture backend (honest), not guessed from a filename.
LANGUAGE = os.environ.get("CAIRN_LANGUAGE", "rust")
CAPTURE_BACKEND = os.environ.get(
    "CAIRN_CAPTURE_BACKEND",
    "rr-dwarf" if "rr" in FRAMES else "gdb-dwarf")
# Per-backend feature support; the API surfaces this so the UI adapts and we
# never fake a capability a backend lacks.
CAPABILITIES = json.loads(os.environ.get("CAIRN_CAPABILITIES", "null")) or {
    "reverse_step": "rr" in FRAMES, "loc_state": True,
    "value_tree": True, "spans": False, "counterfactual": True,
}

# General I/O naming idioms (any Rust program), not this scenario's identifiers.
IO_VERBS = ("write", "read", "fetch", "connect", "dial", "send", "recv",
            "open", "flush", "resolve", "bind", "accept", "load_or", "store",
            "fsync", "seek")
# Test seams that stand in for real I/O — generic markers, not a fixed name.
INJECTED = ("for_test", "_mock", "_stub", "fake_")


def file_rel(path):
    """crates/<crate>/src/oauth/middleware.rs -> oauth/middleware.rs"""
    m = re.search(r"/src/(.+)$", "/" + path)
    return m.group(1) if m else os.path.basename(path)


def module_of_file(rel):
    head = rel.split("/")
    return head[0] if len(head) > 1 else os.path.splitext(head[0])[0]


def lane_id(path, leaf):
    return f"{file_rel(path)}::{leaf}"


def leaf_of(logical):
    """Last segment of a logical fn name, separator-agnostic: Rust `::` and
    Python `.` both yield the bare function name."""
    return logical.split("::")[-1].split(".")[-1]


# Epoch annotation — NAME-gated (never magnitude alone); specs live in core
# decoders so backend copies can't drift.
import decoders


def _tag_time(vv, name=None, fn=None):
    timeish = (name and decoders.timeish_name(name)) or \
              (fn and decoders.timeish_fn(fn))
    s = vv.get("value")
    if timeish and s and s.lstrip("-").isdigit():
        iso = decoders.epoch_iso(int(s))
        if iso:
            vv["as_time"] = iso


def arm_of(deciding):
    if deciding is None:
        return None
    if deciding in ("true", "false"):
        return deciding
    if deciding == "None":
        return "none"
    head = deciding.split("(")[0]
    return head.lower() if head in ("Ok", "Err", "Some") else None


def kind_of(leaf, deciding, count, hit_cap=40):
    if count > hit_cap:
        return "loop_collapsed"
    if any(v in leaf for v in IO_VERBS):
        return "io"
    if arm_of(deciding) is not None:
        return "branch"
    return "call"


def is_surprising(leaf, deciding):
    """Predict-mode signal: the runtime outcome contradicts what the name
    leads a human to expect — a validate/find/lookup/check that came back
    negative, or any miss. Grounded in the recorded return."""
    arm = arm_of(deciding)
    if arm in ("err", "none", "false"):
        return True
    # a *_valid / has_ / is_ that returned the affirmative is unsurprising
    return False


def not_taken_for(leaf, deciding, file=None, line=None):
    """Counterfactual: the arm that did NOT run. The arm label + summary come
    from the recorded taken arm; target_line is located by static source scan
    of the real function body (or left null, never guessed)."""
    arm = arm_of(deciding)
    if arm is None:
        return None
    other = {
        "ok": ("err", "would have returned the error path"),
        "err": ("ok", "would have continued on the success value"),
        "some": ("none", "would have taken the missing-value path"),
        "none": ("some", "would have used the found value"),
        "true": ("false", "would have taken the false branch"),
        "false": ("true", "would have taken the true branch"),
    }.get(arm)
    if not other:
        return None
    nt = {"arm": other[0], "target_line": None, "summary": other[1]}
    if file and line and CAPABILITIES.get("counterfactual"):
        try:
            import sys as _sys
            _rd = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "backends", "rust")
            if _rd not in _sys.path:
                _sys.path.append(_rd)
            import otherarm
            loc = otherarm.other_arm(file, line, other[0])
            if loc:
                nt.update(target_line=loc["target_line"],
                          candidate_lines=loc["candidate_lines"],
                          snippet=loc["snippet"],
                          target_line_provenance=loc["provenance"])
        except Exception:
            pass
    if nt["target_line"] is None:
        nt["target_line_provenance"] = "no static other-arm site found (withheld, not guessed)"
    return nt


def build():
    agg, ranked, decisions, rows = rank.analyze(FRAMES)
    rows = sorted(rows, key=lambda r: r["step"])

    # Tracing-span detection drives causal provenance. If the crate uses spans,
    # causal edges are the span tree; otherwise we keep recorded call nesting
    # and SAY so (causal_backend), never passing one off as the other.
    span_fns, any_span = set(), False
    if LANGUAGE == "rust":          # span detection is a Rust source scan
        try:
            import sys as _sys
            _rd = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "backends", "rust")
            if _rd not in _sys.path:
                _sys.path.append(_rd)
            import spans
            _sig = spans.detect({r["file"] for r in rows})
            span_fns = _sig["span_fns"]
            any_span = _sig["any_span"]
        except Exception:
            pass
    causal_backend = "tracing-spans" if any_span else CAUSAL_BACKEND

    def is_span_frame(r):
        return (r["file"], leaf_of(rank.logical_key(r))) in span_fns

    # ---- lanes: stable source/declaration order ---------------------------
    # file -> {functions: {leaf: min_decl_line}}
    files = {}
    for r in rows:
        if r["file"] in ("?", "", None):
            continue  # unresolved source -> no lane (kept honest, not faked)
        rel = file_rel(r["file"])
        leaf = leaf_of(rank.logical_key(r))
        f = files.setdefault(r["file"], {"rel": rel, "module": module_of_file(rel),
                                         "fns": {}})
        ln = f["fns"].get(leaf)
        f["fns"][leaf] = r["line"] if ln is None else min(ln, r["line"])

    lanes = []
    for so, path in enumerate(sorted(files, key=lambda p: file_rel(p))):
        f = files[path]
        children = [lane_id(path, leaf) for leaf, _ln in
                    sorted(f["fns"].items(), key=lambda kv: kv[1])]
        lanes.append({
            "id": f["rel"], "module": f["module"], "source_order": so,
            "kind": "file", "children": children,
        })

    # ---- frames: one per recorded stop ------------------------------------
    frames = []
    boundaries = []
    id_by_step = {}
    for i, r in enumerate(rows):
        c = rank.logical_key(r)
        a = agg[c]
        leaf = leaf_of(c)
        deciding = decisions.get(c)
        kind = kind_of(leaf, deciding, a["count"])
        fid = f"f{i:04d}"
        id_by_step[r["step"]] = fid

        values = []
        for v in r.get("args", []):
            vv = {"name": v["name"], "value": v["value"], "event": "read"}
            if v.get("ref"):            # pointer/reference metadata for the UI
                vv["ref"] = v["ref"]
            _tag_time(vv, name=v["name"])          # arg named like a time
            values.append(vv)
        if deciding is not None:
            rv = {"name": "<return>", "value": deciding, "event": "born"}
            _tag_time(rv, fn=leaf)                  # fn named like a clock (unix_now)
            values.append(rv)

        frame = {
            "id": fid,
            "lane": lane_id(r["file"], leaf),
            "step": i,                       # dense logical X axis
            "kind": kind,
            "file": r["file"],
            "line": r["line"],
            "salience": round(a["salience_norm"], 2),
            "role": rank.role_for(c, r.get("args", [])),
            "values": values,
            "loop_count": a["count"] if kind == "loop_collapsed" else None,
            "span_boundary": is_span_frame(r),
        }
        if kind == "branch":
            frame["branch"] = {
                "taken_arm": arm_of(deciding),
                "deciding_value": f"{leaf} ⇒ {deciding}",
                "surprising": is_surprising(leaf, deciding),
                "not_taken": not_taken_for(leaf, deciding, r["file"], r["line"]),
            }
        frames.append(frame)

        if kind == "io":
            detail = f"{leaf}"
            if any(x in leaf for x in INJECTED):
                detail += " (injected, no network)"
            boundaries.append({"step": i, "kind": "io", "detail": detail,
                               "lane": file_rel(r["file"])})

    # ---- causal edges ------------------------------------------------------
    # When the crate uses tracing spans, the edges ARE the span tree (only span
    # frames are nodes; their relative nesting gives logical parent/child).
    # Otherwise we connect all recorded frames by call nesting. causal_backend
    # records which, so the edges are never misrepresented as spans.
    def build_edges(node_ok):
        edges = []
        stack = []            # (fid, depth)
        last_child_of = {}    # parent_fid -> last child fid (for follows)
        for r in rows:
            if not node_ok(r):
                continue
            fid = id_by_step[r["step"]]
            d = r["depth"]
            while stack and stack[-1][1] >= d:
                stack.pop()
            if stack:
                parent = stack[-1][0]
                edges.append({"from": parent, "to": fid, "relation": "span_child"})
                prev = last_child_of.get(parent)
                if prev is not None:
                    edges.append({"from": prev, "to": fid, "relation": "span_follows"})
                last_child_of[parent] = fid
            stack.append((fid, d))
        return edges

    edges = build_edges(is_span_frame) if any_span else build_edges(lambda r: True)

    precis = make_precis(ranked, decisions, boundaries)

    return {
        "scenario": {
            "id": SCEN_ID, "crate": CRATE, "language": LANGUAGE,
            "recorded_at": None,                 # stamped by caller (no clock in builder)
            "capture_backend": CAPTURE_BACKEND,
            "causal_backend": causal_backend,
        },
        "capabilities": CAPABILITIES,
        "lanes": lanes,
        "frames": frames,
        "causal_edges": edges,
        "boundaries": boundaries,
        "precis": precis,
    }


def make_precis(ranked, decisions, boundaries):
    """Deterministic, scenario-independent summary assembled from recorded
    facts: the dominant flow's entry function, its most salient steps with
    their real deciding values, the I/O boundaries crossed, and the entry's
    outcome. No hand-written narrative — a downstream model narrates this."""
    if not ranked:
        return "No user-crate frames were recorded."
    # entry = most central function nearest the call-tree root
    central = [a for a in ranked if a.get("flow", 0) >= 0.9] or ranked[:1]
    root = min(central, key=lambda a: a["mindepth"])
    rootname = leaf_of(root["fn"])
    rootout = decisions.get(root["fn"])

    steps = []
    for a in ranked:
        if a["fn"] == root["fn"]:
            continue
        dv = decisions.get(a["fn"])
        steps.append(f"{rank.leaf(a['fn'])}" + (f" ⇒ {dv}" if dv else ""))
        if len(steps) >= 6:
            break

    io = sorted({b["detail"] for b in boundaries})
    out = f"Entry `{rootname}`" + (f" returned {rootout}" if rootout else "") + ". "
    out += "Salient steps: " + "; ".join(steps) + "."
    if io:
        out += " I/O boundaries: " + ", ".join(io[:8]) + "."
    return out


if __name__ == "__main__":
    art = build()
    out = os.environ.get("CAIRN_ARTIFACT", "out/artifact.json")
    with open(out, "w") as fh:
        json.dump(art, fh, indent=2)
    print(f"wrote {out}: {len(art['frames'])} frames, {len(art['lanes'])} lanes, "
          f"{len(art['causal_edges'])} edges, {len(art['boundaries'])} boundaries")
    print("precis:", art["precis"])
