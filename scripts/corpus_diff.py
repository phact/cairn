#!/usr/bin/env python3
"""
Compute sibling DIFFS for corpus traces — the labeled bucket.

tracediff.diff() is wired to the main .cairn/traces layout; this reuses its
exact flip logic (per-activation arm sequences -> deciding frames) but loads
from corpus/<repo>/<test>/. For each repo it diffs every pair within a sibling
group (tests sharing a top-module prefix, which exercise overlapping code with
different inputs) and writes corpus/<repo>/diffs.json.

A "flip" = a function that ran in BOTH sibling traces but returned a different
arm at the same activation index = ground-truth decider label (by construction).
"""
import json
import os
import sys
import itertools
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, ".cairn", "corpus")
sys.path.insert(0, os.path.join(ROOT, "cairn"))
import rank
import artifact as A
import tracediff          # reuse _classify


def _logical(r):
    return rank.logical_key(r)


def load(repo, test):
    """Load one corpus trace -> arm sequences + presence set + parents."""
    d = os.path.join(CORPUS, repo, test)
    fp = os.path.join(d, "frames.jsonl")
    rows = sorted((json.loads(l) for l in open(fp)), key=lambda r: r["step"])
    seq = [_logical(r) for r in rows]
    raw = []
    if os.path.exists(fp + ".returns"):
        try: raw = json.load(open(fp + ".returns"))
        except Exception: raw = []
    raw.sort(key=lambda r: (r.get("enter_step") is None, r.get("enter_step") or 0))
    seqs = defaultdict(list)
    shows = defaultdict(dict)
    for r in raw:
        fn = _logical(r)
        if r.get("outcome"):
            arm = r["outcome"].get("arm"); disp = r["outcome"].get("display")
        else:
            disp = rank.concise_return(r.get("value")); arm = A.arm_of(disp)
        if arm:
            seqs[fn].append(arm); shows[fn][arm] = disp
    parents = defaultdict(set); stack = []
    for r in rows:
        d_, c = r["depth"], _logical(r)
        while stack and stack[-1][1] >= d_:
            stack.pop()
        if stack:
            parents[c].add(stack[-1][0])
        stack.append((c, d_))
    return {"seq": seq, "seqs": dict(seqs), "shows": dict(shows),
            "parents": dict(parents), "has_arms": bool(raw)}


def diff_pair(repo, ta, tb):
    Ad, Bd = load(repo, ta), load(repo, tb)
    deciding = []
    for fn in sorted(set(Ad["seqs"]) & set(Bd["seqs"])):
        sa, sb = Ad["seqs"][fn], Bd["seqs"][fn]
        kind, k = tracediff._classify(sa, sb)
        if kind == "flip":
            deciding.append({"fn": fn, "flip_at_activation": k,
                             "a_arm": sa[k], "b_arm": sb[k]})
    fa, fb = set(Ad["seq"]), set(Bd["seq"])
    both = fa & fb
    frontier = ([{"fn": f, "side": "a"} for f in sorted(fa - fb)
                 if Ad["parents"].get(f, set()) & both] +
                [{"fn": f, "side": "b"} for f in sorted(fb - fa)
                 if Bd["parents"].get(f, set()) & both])
    return {"a": ta, "b": tb, "a_has_arms": Ad["has_arms"],
            "b_has_arms": Bd["has_arms"],
            "deciding_frames": deciding, "presence_frontier": frontier,
            "n_deciders": len(deciding), "n_frontier": len(frontier)}


GROUP_CAP = 6          # tests per repo to arm+diff (bounds C(n,2) pairs)


def sibling_groups(repo):
    """All usable tests of a repo as ONE group — any two tests of the same crate
    exercise its core functions, so all pairs are diffable (non-overlapping
    tests simply yield few flips). Capped to bound the arms cost and pair count;
    prefer the largest traces (more shared structure -> more potential flips)."""
    rd = os.path.join(CORPUS, repo)
    tests = [(t, sum(1 for _ in open(os.path.join(rd, t, "frames.jsonl"))))
             for t in os.listdir(rd)
             if os.path.exists(os.path.join(rd, t, "frames.jsonl"))]
    usable = [t for t, n in sorted(tests, key=lambda x: -x[1]) if n >= 5]
    return [usable[:GROUP_CAP]] if len(usable) >= 2 else []


def diff_repo(repo):
    groups = sibling_groups(repo)
    pairs = []
    for g in groups:
        for ta, tb in itertools.combinations(sorted(g), 2):
            pairs.append(diff_pair(repo, ta, tb))
    out = {"repo": repo, "n_pairs": len(pairs),
           "total_deciders": sum(p["n_deciders"] for p in pairs),
           "any_arms": any(p["a_has_arms"] or p["b_has_arms"] for p in pairs),
           "pairs": pairs}
    json.dump(out, open(os.path.join(CORPUS, repo, "diffs.json"), "w"), indent=2)
    return out


if __name__ == "__main__":
    r = diff_repo(sys.argv[1])
    print(f"{r['repo']}: {r['n_pairs']} sibling pairs, "
          f"{r['total_deciders']} deciding frames, arms={r['any_arms']}")
    for p in r["pairs"][:6]:
        print(f"  {p['a'][:24]} vs {p['b'][:24]}: "
              f"{p['n_deciders']} flips, {p['n_frontier']} frontier")
