"""
Skeleton-by-grouping (prototype): the recorded dynamic call TREE, expanded under
a row BUDGET instead of a salience threshold.

The frames are activations with an absolute stack `depth`; a sequential
execution is depth-first, so each frame's parent is the nearest preceding frame
of smaller depth, and each subtree occupies a CONTIGUOUS step range. We show K
chunks by greedily expanding the biggest subtree (biggest = most frames ran
under it — a recorded fact) until the frontier reaches K rows; everything still
collapsed shows as a range. No threshold, no per-frame salience.
"""

import json

import layout
import rank


def _load_rows(tid):
    sp = layout.frames_path(tid)
    import os
    if not os.path.exists(sp):
        sp = os.path.join(layout.recording_dir(tid), "frames_py.jsonl")
    rows = [json.loads(l) for l in open(sp)]
    rows = [r for r in rows if r.get("fn") != "syscall_traced"]
    return sorted(rows, key=lambda r: r["step"])


def build_tree(rows):
    """Build the dynamic call tree, then MERGE consecutive siblings that are
    re-entries of the same logical function (async polls of one fn are one
    logical step, like the rest of Cairn's async collapse). Returns
    (nodes, roots) on the merged tree."""
    raw = [{"idx": i, "row": r, "depth": r["depth"], "key": rank.logical_key(r),
            "parent": None, "kids": []} for i, r in enumerate(rows)]
    stack = []
    for n in raw:
        while stack and stack[-1]["depth"] >= n["depth"]:
            stack.pop()
        if stack:
            n["parent"] = stack[-1]["idx"]
            stack[-1]["kids"].append(n["idx"])
        stack.append(n)
    raw_roots = [n["idx"] for n in raw if n["parent"] is None]

    # rebuild as merged nodes: walk each child-list, fuse runs of equal key
    nodes = []

    def emit(child_idxs):
        out = []
        i = 0
        cs = sorted(child_idxs, key=lambda j: raw[j]["row"]["step"])
        while i < len(cs):
            j = i
            key = raw[cs[i]]["key"]
            while j + 1 < len(cs) and raw[cs[j + 1]]["key"] == key:
                j += 1
            members = cs[i:j + 1]                 # async polls of one fn
            nid = len(nodes)
            base = raw[members[0]]
            node = {"idx": nid, "row": base["row"], "children": [],
                    "step_lo": base["row"]["step"],
                    "step_hi": raw[members[-1]]["row"]["step"],
                    "subtree": 0, "lines": {}, "_members": members}
            nodes.append(node)
            kid_idxs = [k for m in members for k in raw[m]["kids"]]
            node["children"] = emit(kid_idxs)
            for c in node["children"]:
                nodes[c]["parent"] = nid
            out.append(nid)
            i = j + 1                          # advance past the merged run
        return out

    roots = emit(raw_roots)

    # post-order metrics
    order = sorted(range(len(nodes)), key=lambda i: -nodes[i]["row"]["depth"])
    for i in order:
        n = nodes[i]
        n["subtree"] = len(n["_members"]) + sum(nodes[c]["subtree"] for c in n["children"])
        n["step_hi"] = max([n["step_hi"]] + [nodes[c]["step_hi"] for c in n["children"]])
        f, ln = n["row"].get("file"), n["row"].get("line")
        if f and ln:
            lo, hi = n["lines"].get(f, (ln, ln))
            n["lines"][f] = (min(lo, ln), max(hi, ln))
        for c in n["children"]:
            for f, (lo, hi) in nodes[c]["lines"].items():
                plo, phi = n["lines"].get(f, (lo, hi))
                n["lines"][f] = (min(plo, lo), max(phi, hi))
    return nodes, roots


def skeleton(tid, rows_budget=12):
    """Frontier of ~rows_budget chunks, biggest-subtree-first expansion."""
    rows = _load_rows(tid)
    nodes, roots = build_tree(rows)
    frontier = list(roots)                    # each is a collapsed chunk (row)
    # Greedily expand the biggest expandable chunk until we reach the budget.
    # (If there are already > budget roots, we show them all — can't go below
    # the forest's root count without inventing a parent.)
    while len(frontier) < rows_budget:
        cand = [i for i in frontier if nodes[i]["children"]]
        if not cand:
            break
        big = max(cand, key=lambda i: nodes[i]["subtree"])
        frontier.remove(big)
        frontier.extend(nodes[big]["children"])   # monotonic: may reach/pass K
    frontier.sort(key=lambda i: nodes[i]["step_lo"])   # execution order
    return [_chunk(nodes[i], nodes) for i in frontier], len(rows)


def _chunk(n, rows):
    r = n["row"]
    lane = f"{_rel(r.get('file'))}::{rank.leaf(rank.logical_key(r))}"
    # the tightest single line-range if local; else the count of files touched
    files = n["lines"]
    if len(files) == 1:
        f, (lo, hi) = next(iter(files.items()))
        linespan = f"{_rel(f)}:{lo}-{hi}" if hi > lo else f"{_rel(f)}:{lo}"
    else:
        linespan = f"{len(files)} files"
    return {"lane": lane, "fn": rank.logical_key(r),
            "step_range": [n["step_lo"], n["step_hi"]],
            "frames": n["subtree"], "lines": linespan,
            "collapsed": bool(n["children"])}


def _rel(p):
    if not p:
        return "?"
    import re
    m = re.search(r"/src/(.+)$", "/" + p)
    return m.group(1) if m else p.split("/")[-1]


# ---- measurement helpers ----------------------------------------------------
def _frontier(nodes, roots, rows_budget):
    frontier = list(roots)
    while len(frontier) < rows_budget:
        cand = [i for i in frontier if nodes[i]["children"]]
        if not cand:
            break
        big = max(cand, key=lambda i: nodes[i]["subtree"])
        frontier.remove(big)
        frontier.extend(nodes[big]["children"])
    return frontier


def _subtree_leaves(nodes, i, out):
    out.add(rank.leaf(rank.logical_key(nodes[i]["row"])))
    for c in nodes[i]["children"]:
        _subtree_leaves(nodes, c, out)


def shown_with_highlights(tid, rows_budget, top_m, salience):
    """Leaves on screen = every chunk + every expanded header + the top-M most
    SALIENT interior frames of each visible chunk (the ranker's key frames shown
    as the chunk's headline, without spending a row). `salience` is leaf->score."""
    rows = _load_rows(tid)
    nodes, roots = build_tree(rows)
    frontier = _frontier(nodes, roots, rows_budget)
    shown = set()
    for i in frontier:
        j = i                                   # the chunk + its header chain
        while j is not None:
            shown.add(rank.leaf(rank.logical_key(nodes[j]["row"])))
            j = nodes[j].get("parent")
        interior = set()                        # top-M salient frames inside
        _subtree_leaves(nodes, i, interior)
        for lf in sorted(interior, key=lambda l: -salience.get(l, 0))[:top_m]:
            shown.add(lf)
    return shown, len(frontier)


def shown_leaves(tid, rows_budget):
    """The leaf-names ON SCREEN at this budget: every frontier chunk PLUS every
    expanded-through ancestor (a header is a visible, clickable row in a tree
    view). This is the apples-to-apples 'is it visible' for the tree."""
    rows = _load_rows(tid)
    nodes, roots = build_tree(rows)
    frontier = _frontier(nodes, roots, rows_budget)
    shown = set()
    for i in frontier:
        j = i
        while j is not None:
            shown.add(rank.leaf(rank.logical_key(nodes[j]["row"])))
            j = nodes[j].get("parent")
    return shown, len(frontier)
