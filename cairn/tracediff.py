"""
Cairn trace diff — compare two recordings of related code and extract the
DIVERGENCE FRONTIER: where and why the runs differ.

Three channels, all recorded ground truth (alignment is positional, never
semantic guessing):

  1. outcome divergence — the same logical function executed in BOTH runs but
     returned different arms (validate ⇒ Ok vs ⇒ Err). These are the deciding
     frames: ground truth by construction for "why did run B do X when run A
     didn't".
  2. presence divergence — functions that executed in only one run: the
     downstream consequences of the decision (the road that became real).
  3. path divergence — where the aligned call sequences first split.

This doubles as benchmark v0 for the ranker: a deciding frame is exactly what
a senior engineer would point at, so `ranker_check` reports where each one
landed in the salience ranking (deciding-frame rank = the metric).
"""

import difflib
import json
import os

import layout
import rank
import artifact as artifact_mod


class DiffError(Exception):
    pass


# ---- loading ----------------------------------------------------------------
def _stream_path(tid):
    """The frame stream for a trace, whichever backend produced it."""
    cands = [layout.frames_path(tid),
             os.path.join(layout.recording_dir(tid), "frames_py.jsonl")]
    for p in cands:
        if os.path.exists(p):
            return p
    raise DiffError(f"no frame stream for `{tid}`")


def _load(tid):
    sp = _stream_path(tid)
    rows = sorted((json.loads(l) for l in open(sp)), key=lambda r: r["step"])
    seq = [rank.logical_key(r) for r in rows]
    try:
        raw = json.load(open(sp + ".returns"))
    except (OSError, json.JSONDecodeError):
        raw = []
    # per-activation arm SEQUENCES in enter order — sets hide whether a
    # difference is a real flip or just extra downstream activations
    seqs = {}      # logical fn -> [arm, arm, ...] ordered by enter_step
    shows = {}     # logical fn -> representative display per arm
    raw_sorted = sorted(raw, key=lambda r: (r.get("enter_step") is None,
                                            r.get("enter_step") or 0))
    for r in raw_sorted:
        fn = rank.logical_key(r)
        if r.get("outcome"):
            arm = r["outcome"].get("arm")
            disp = r["outcome"].get("display")
        else:
            disp = rank.concise_return(r.get("value"))
            arm = artifact_mod.arm_of(disp)
        if arm:
            seqs.setdefault(fn, []).append(arm)
            shows.setdefault(fn, {})[arm] = disp
    arms = {fn: set(s) for fn, s in seqs.items()}
    # caller sets from real depth nesting (for flip subsumption + frontiers)
    parents = {}
    stack = []
    for r in rows:
        d = r["depth"]
        c = rank.logical_key(r)
        while stack and stack[-1][1] >= d:
            stack.pop()
        if stack:
            parents.setdefault(c, set()).add(stack[-1][0])
        stack.append((c, d))
    art = None
    ap = layout.artifact_path(tid)
    if os.path.exists(ap):
        art = json.load(open(ap))
    return {"tid": tid, "rows": rows, "seq": seq, "arms": arms,
            "seqs": seqs, "shows": shows, "artifact": art, "parents": parents}


def _classify(sa, sb):
    """Cause vs consequence. `flip`: activation k exists in BOTH runs with a
    different arm — the same call genuinely decided differently (strong label).
    `count`: the common prefix of arm sequences agrees and only the activation
    COUNT differs — an artifact of one run executing more downstream (weak)."""
    for i in range(min(len(sa), len(sb))):
        if sa[i] != sb[i]:
            return "flip", i
    return ("count", None) if len(sa) != len(sb) else ("same", None)


# ---- diff -------------------------------------------------------------------
def diff(tid_a, tid_b):
    A, B = _load(tid_a), _load(tid_b)

    # 1. outcome divergence: executed in both, different recorded outcomes —
    #    split into FLIPS (cause: same activation, different arm) and COUNT
    #    artifacts (consequence: extra activations downstream of the decision)
    deciding, consequences = [], []
    for fn in sorted(set(A["seqs"]) & set(B["seqs"])):
        sa, sb = A["seqs"][fn], B["seqs"][fn]
        kind, k = _classify(sa, sb)
        if kind == "same":
            continue
        item = {
            "fn": fn, "kind": kind,
            "a_arms": sorted(set(sa)), "b_arms": sorted(set(sb)),
            "a_value": next(iter(A["shows"][fn].values()), None),
            "b_value": next(iter(B["shows"][fn].values()), None),
        }
        if kind == "flip":
            item["flip_at_activation"] = k
            item["a_arm_at_flip"], item["b_arm_at_flip"] = sa[k], sb[k]
            deciding.append(item)
        else:
            item["a_activations"], item["b_activations"] = len(sa), len(sb)
            consequences.append(item)

    # SUBSUMPTION: a flip whose caller is also a flip is the MECHANISM of the
    # caller's decision, not an independent decider (eq_ascii_ci under
    # extract_jwt). Labels change; scoring never loosens.
    flip_fns = {c["fn"] for c in deciding}
    # exclude SELF from callers: a closure desugars to its parent's logical
    # name, which would make a function its own caller and self-subsume.
    callers = {fn: ((A["parents"].get(fn, set()) | B["parents"].get(fn, set()))
                    - {fn})
               for fn in flip_fns}
    primary, mechanism = [], []
    for c in deciding:
        by = sorted(callers[c["fn"]] & flip_fns)
        if by:
            c["subsumed_by"] = by
            mechanism.append(c)
        else:
            primary.append(c)
    deciding = primary

    # 2. presence divergence (+ FRONTIER: the first only-in-X functions, i.e.
    #    those whose caller executed in BOTH runs — the road that became real;
    #    their transitive callees are downstream, not labels)
    fa, fb = set(A["seq"]), set(B["seq"])
    only_a = sorted(fa - fb)
    only_b = sorted(fb - fa)
    both = fa & fb

    def _is_glue(fn):
        # EXACT match only: prefix matching ate real fns (`next_probe_id`
        # swallowed by iterator-glue `next`).
        lf = artifact_mod.leaf_of(fn)
        return lf in rank.PLUMBING or "{constructor" in fn

    frontier = (
        [{"fn": f, "side": "a"} for f in only_a
         if A["parents"].get(f, set()) & both and not _is_glue(f)] +
        [{"fn": f, "side": "b"} for f in only_b
         if B["parents"].get(f, set()) & both and not _is_glue(f)])

    # 3. path divergence: positional alignment of the call sequences
    sm = difflib.SequenceMatcher(None, A["seq"], B["seq"], autojunk=False)
    blocks = sm.get_opcodes()
    matched = sum(i2 - i1 for op, i1, i2, j1, j2 in blocks if op == "equal")
    first_split = next(((op, i1, i2, j1, j2) for op, i1, i2, j1, j2 in blocks
                        if op != "equal"), None)
    split = None
    if first_split:
        op, i1, i2, j1, j2 = first_split
        split = {"a_step": i1, "b_step": j1,
                 "a_next": A["seq"][i1:i1 + 3], "b_next": B["seq"][j1:j1 + 3]}

    # ranker check: primary flips (both sides) + presence frontier (its side)
    checks = [c for fn in deciding
              if (c := _ranker_check(fn["fn"], A, B)) is not None]
    presence_checks = []
    for pf in frontier:
        side = A if pf["side"] == "a" else B
        c = _ranker_check(pf["fn"], side, side)
        if c and "a" in c:
            presence_checks.append({"fn": pf["fn"], "side": pf["side"],
                                    "rank": c["a"]["rank"], "of": c["a"]["of"]})

    return {
        "a": {"trace": tid_a, "frames": len(A["rows"])},
        "b": {"trace": tid_b, "frames": len(B["rows"])},
        "aligned_fraction": round(2 * matched / max(1, len(A["seq"]) + len(B["seq"])), 3),
        "deciding_frames": deciding,            # primary flips (cause)
        "mechanism_flips": mechanism,           # flips subsumed by a calling flip
        "outcome_consequences": consequences,   # count artifacts (downstream)
        "only_in_a": only_a, "only_in_b": only_b,
        "presence_frontier": frontier,          # first only-in-X fns (2nd label class)
        "first_split": split,
        "ranker_check": checks,
        "presence_check": presence_checks,
        "grounding": ("deciding_frames are FLIPS: the same activation index "
                      "returned a different arm in each run — cause, not "
                      "consequence. outcome_consequences differ only in "
                      "activation count (downstream of the decision). All "
                      "entries are recorded executions; alignment is "
                      "positional, never semantic."),
    }


def _ranker_check(fn, A, B):
    """Where does this deciding frame land in each trace's salience ranking?
    The benchmark-v0 metric: deciding frames should rank near the top."""
    leaf = artifact_mod.leaf_of(fn)
    out = {"fn": fn}
    found = False
    for side, data in (("a", A), ("b", B)):
        art = data["artifact"]
        if not art:
            continue
        by_lane = {}
        for f in art["frames"]:
            cur = by_lane.get(f["lane"])
            if cur is None or f["salience"] > cur["salience"]:
                by_lane[f["lane"]] = f
        ranked = sorted(by_lane.values(), key=lambda f: -f["salience"])
        for i, f in enumerate(ranked, 1):
            if f["lane"].split("::")[-1] == leaf:
                out[side] = {"salience": f["salience"], "rank": i,
                             "of": len(ranked)}
                found = True
                break
    return out if found else None


# ---- human rendering ----------------------------------------------------------
def render(d):
    L = [f"trace diff",
         f"  A: {d['a']['trace']}  ({d['a']['frames']} frames)",
         f"  B: {d['b']['trace']}  ({d['b']['frames']} frames)",
         f"  call sequences aligned: {d['aligned_fraction'] * 100:.0f}%", ""]
    if d["deciding_frames"]:
        L.append("DECIDING FRAMES — same activation, different recorded arm (cause):")
        for c in d["deciding_frames"]:
            L.append(f"  {c['fn']}  (activation #{c['flip_at_activation']}: "
                     f"A={c['a_arm_at_flip']}  B={c['b_arm_at_flip']})")
            L.append(f"      A: {'/'.join(c['a_arms'])} ({c['a_value']})")
            L.append(f"      B: {'/'.join(c['b_arms'])} ({c['b_value']})")
    else:
        L.append("no outcome flips (same arms at every shared activation)")
    if d.get("outcome_consequences"):
        L.append("")
        L.append("outcome differences from extra activations (consequence, weak):")
        for c in d["outcome_consequences"]:
            L.append(f"  {c['fn']}  A×{c['a_activations']} vs B×{c['b_activations']}")
    if d["first_split"]:
        s = d["first_split"]
        L += ["", f"path split: A step {s['a_step']} / B step {s['b_step']}",
              f"  A continues: {', '.join(s['a_next'])}",
              f"  B continues: {', '.join(s['b_next'])}"]
    if d["only_in_a"]:
        L += ["", f"ran only in A ({len(d['only_in_a'])}):  " +
              ", ".join(artifact_mod.leaf_of(f) for f in d["only_in_a"][:10]) +
              ("…" if len(d["only_in_a"]) > 10 else "")]
    if d["only_in_b"]:
        L += [f"ran only in B ({len(d['only_in_b'])}):  " +
              ", ".join(artifact_mod.leaf_of(f) for f in d["only_in_b"][:10]) +
              ("…" if len(d["only_in_b"]) > 10 else "")]
    if d["ranker_check"]:
        L += ["", "ranker check (deciding frames should rank high):"]
        for c in d["ranker_check"]:
            bits = []
            for side in ("a", "b"):
                if side in c:
                    r = c[side]
                    mark = "✓" if r["rank"] <= max(5, r["of"] // 4) else "✗"
                    bits.append(f"{side.upper()}: #{r['rank']}/{r['of']} "
                                f"(sal {r['salience']}) {mark}")
            L.append(f"  {c['fn']}:  " + "   ".join(bits))
    return "\n".join(L)
