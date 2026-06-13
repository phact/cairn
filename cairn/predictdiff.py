"""
predict-the-diff: the mental-model metric.

Show a fresh agent ONLY a structural skeleton of a recorded run — outcomes
HIDDEN — and ask it to predict which functions' return value would change if
inputs changed (the decision points). Score against the ACTUAL deciding frames
from diffing this trace with its siblings. The representation that yields better
divergence-prediction is the better mental model — measured, not judged.

Two representations under test, both arms-hidden, same budget:
  flat     — the salience-ranked list (structural fields only)
  grouped  — the call tree (chunks + ranked key-frames inside), ranges
"""

import itertools
import json

import layout
import tracediff
import grouping
import rank
import artifact as A


def ground_truth(tid):
    """Deciding-frame leaves for `tid`: functions that FLIP outcome between this
    trace and any sibling (primary flips, subsumption applied). Ground truth by
    construction — never shown to the predictor."""
    crate = tid.split("::", 1)[0]
    sibs = [t for t in layout.list_traces()
            if t.split("::", 1)[0] == crate and t != tid]
    decided = set()
    for s in sibs:
        d = tracediff.diff(tid, s)
        for c in d["deciding_frames"]:
            decided.add(A.leaf_of(c["fn"]))
    return decided


def _structural(tid):
    """leaf -> {fanout, fanin, count, salience, file, line}, from the ranker's
    own aggregate — the structural signals, never the outcome arm."""
    agg, ranked, decisions, rows = rank.analyze(layout.frames_path(tid))
    out = {}
    for a in agg.values():
        out[rank.leaf(a["fn"])] = {
            "fanout": a.get("fanout", 0), "fanin": a.get("fanin", 0),
            "count": a.get("count", 1), "salience": a.get("salience_norm", 0),
            "file": a["file"].split("/")[-1], "line": a["line"]}
    return out, ranked


def _sal_map(tid):
    st, _ = _structural(tid)
    return {k: v["salience"] for k, v in st.items()}, None


def flat_text(tid, k=12):
    """Salience-ranked functions — STRUCTURAL fields only, no outcome arm."""
    st, ranked = _structural(tid)
    top = sorted(st.items(), key=lambda kv: -kv[1]["salience"])[:k]
    L = ["A recorded execution, shown as its most structurally-central functions",
         "(ranked by how central each is to the call flow; NO outcomes shown):", ""]
    for i, (leaf, s) in enumerate(top, 1):
        L.append(f"  {i:>2}. {leaf:<34} {s['file']}:{s['line']:<4} "
                 f"calls-out={s['fanout']} called-from={s['fanin']} "
                 f"ran×{s['count']}")
    return "\n".join(L)


def grouped_text(tid, k=12, top_m=3):
    """The call tree at budget k, chunks with ranked key-frames inside — names
    only, NO outcome arm."""
    sal, _ = _sal_map(tid)
    rows = grouping._load_rows(tid)
    nodes, roots = grouping.build_tree(rows)
    frontier = grouping._frontier(nodes, roots, k)
    frontier.sort(key=lambda i: nodes[i]["step_lo"])
    L = ["A recorded execution, shown as its call structure",
         "(chunks = subtrees; ×N = functions ran under it; NO outcomes shown):", ""]
    for i in frontier:
        n = nodes[i]
        leaf = rank.leaf(rank.logical_key(n["row"]))
        f = n["row"].get("file", "?").split("/")[-1]
        ln = n["row"].get("line", "?")
        interior = set()
        grouping._subtree_leaves(nodes, i, interior)
        interior.discard(leaf)
        keys = sorted(interior, key=lambda l: -sal.get(l, 0))[:top_m]
        ktxt = f"   [contains: {', '.join(keys)}]" if keys else ""
        L.append(f"  {leaf:<32} {f}:{ln:<4} steps {n['step_lo']}-{n['step_hi']} "
                 f"×{n['subtree']}{ktxt}")
    return "\n".join(L)


def full_text(tid):
    """NO ranking, NO grouping — the entire recorded frame stream as an indented
    call trace (every frame, in order), arms hidden. The 'just hand the model
    everything' baseline."""
    rows = grouping._load_rows(tid)
    mind = min(r["depth"] for r in rows)
    L = ["A recorded execution, shown as its COMPLETE call trace — every function",
         "activation in order, indented by call depth (NO outcomes shown):", ""]
    for r in rows:
        ind = "  " * min(r["depth"] - mind, 12)
        leaf = rank.leaf(rank.logical_key(r))
        f = r.get("file", "?").split("/")[-1]
        L.append(f"  {r['step']:>3} {ind}{leaf}  {f}:{r.get('line','?')}")
    return "\n".join(L)


TASK = (
    "\n\nThis is a real recorded run of a Rust request handler. You CANNOT see "
    "what any function returned. Using ONLY the structure above, predict the "
    "DECISION POINTS: the functions whose return value would most likely be "
    "DIFFERENT if the program's inputs changed (e.g. a different request, an "
    "invalid token, a missing route) — the places where behavior forks.\n"
    "Return STRICT JSON: {\"decision_points\": [\"fn_leaf_name\", ...]} with at "
    "most 6 names, most-likely first. Bare function leaf names only.")


def score(predicted, truth):
    pred = [p for p in predicted]
    hit = [p for p in pred if p in truth]
    prec = len(set(hit)) / len(set(pred)) if pred else 0.0
    rec = len(set(hit)) / len(truth) if truth else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"precision": round(prec, 3), "recall": round(rec, 3),
            "f1": round(f1, 3), "hit": sorted(set(hit)),
            "missed": sorted(truth - set(pred))}


if __name__ == "__main__":
    import sys
    tid = sys.argv[1] if len(sys.argv) > 1 else layout.resolve_active()
    print("=== GROUND TRUTH deciders:", sorted(ground_truth(tid)), "===\n")
    print("--- FLAT ---\n" + flat_text(tid) + TASK)
    print("\n--- GROUPED ---\n" + grouped_text(tid) + TASK)
