"""
Cairn ranker benchmark (v0) — free labels from trace divergence.

Every pair of traces over shared code yields DECIDING FRAMES (outcome flips:
same activation, different recorded arm) — ground truth by construction for
"what would a senior engineer point at to explain the difference". The ranker's
job is to put those frames near the top, so the metric is their rank:

Metrics (named honestly — none of these is classic IR recall except macro):
  MRR          mean(1/rank) over PER-SIDE labels — each trace's ranking judged
               alone; no best-of-both-sides flattery
  hit@5/@10    fraction of per-side labels ranked in the top 5 / 10
  dedup MRR    each deciding fn counted once (its best rank anywhere) — stops a
               frequent easy flip (handle_inner) from padding the score
  macro r@5    true recall@5 per pair, macro-averaged

One number per ranker change — tuning stops being vibes.
"""

import itertools
import json

import layout
import tracediff


def _groups():
    """Traces that can be paired: same crate prefix (shared code)."""
    by_crate = {}
    for t in layout.list_traces():
        crate = t.split("::", 1)[0]
        by_crate.setdefault(crate, []).append(t)
    return {c: ts for c, ts in by_crate.items() if len(ts) >= 2}


def run(threshold_top=5):
    labels = []          # flip labels, PER-SIDE
    plabels = []         # presence-frontier labels (their side only)
    pairs = []
    for crate, traces in sorted(_groups().items()):
        for a, b in itertools.combinations(sorted(traces), 2):
            try:
                d = tracediff.diff(a, b)
            except tracediff.DiffError:
                continue
            pair = {"a": a, "b": b,
                    "flips": len(d["deciding_frames"]),
                    "aligned": d["aligned_fraction"]}
            pairs.append(pair)
            pair_best = []
            for c in d["ranker_check"]:
                ranks = [c[s]["rank"] for s in ("a", "b") if s in c]
                if not ranks:
                    continue
                pname = f"{a.split('::')[1][:24]}⇄{b.split('::')[1][:24]}"
                for s in ("a", "b"):           # PER-SIDE: each ranking alone
                    if s in c:
                        labels.append({"pair": pname, "side": s, "fn": c["fn"],
                                       "rank": c[s]["rank"], "of": c[s]["of"]})
                pair_best.append(min(ranks))
            pair["per_pair_recall5"] = (round(sum(1 for r in pair_best if r <= 5)
                                              / len(pair_best), 3)
                                        if pair_best else None)
            for pc in d.get("presence_check", []):
                plabels.append({"pair": f"{a.split('::')[1][:24]}⇄{b.split('::')[1][:24]}",
                                "fn": pc["fn"], "side": pc["side"],
                                "rank": pc["rank"], "of": pc["of"]})
    if not labels:
        return {"pairs": pairs, "labels": [], "mrr": None}
    rs = [l["rank"] for l in labels]
    best_by_fn = {}
    for l in labels:
        best_by_fn[l["fn"]] = min(best_by_fn.get(l["fn"], 10**9), l["rank"])
    dedup = list(best_by_fn.values())
    macro = [p["per_pair_recall5"] for p in pairs
             if p.get("per_pair_recall5") is not None]
    return {"pairs": pairs, "labels": labels, "n_labels": len(labels),
            "mrr": round(sum(1 / r for r in rs) / len(rs), 3),
            "hit@5": round(sum(1 for r in rs if r <= 5) / len(rs), 3),
            "hit@10": round(sum(1 for r in rs if r <= 10) / len(rs), 3),
            "dedup_mrr": round(sum(1 / r for r in dedup) / len(dedup), 3),
            "n_fns": len(dedup),
            "macro_recall@5": (round(sum(macro) / len(macro), 3) if macro else None),
            "pctl": round(sum(l["rank"] / l["of"] for l in labels)
                          / len(labels), 3),
            "presence_labels": plabels,
            "presence_pctl": (round(sum(l["rank"] / l["of"] for l in plabels)
                                    / len(plabels), 3) if plabels else None),
            "presence_mrr": (round(sum(1 / l["rank"] for l in plabels)
                                   / len(plabels), 3) if plabels else None),
            "presence_hit@5": (round(sum(1 for l in plabels if l["rank"] <= 5)
                                     / len(plabels), 3) if plabels else None),
            "n_presence": len(plabels)}


def render(res):
    L = [f"ranker benchmark — {len(res['pairs'])} trace pairs, "
         f"{res.get('n_labels', 0)} deciding-frame labels", ""]
    for l in res["labels"]:
        mark = "✓" if l["rank"] <= 5 else ("·" if l["rank"] <= 10 else "✗")
        L.append(f"  {mark} #{l['rank']:<3}({l['side']}) {l['fn']:<46} [{l['pair']}]")
    if res["mrr"] is not None:
        L += ["", f"  per-side: MRR {res['mrr']}  hit@5 {res['hit@5']}  "
                  f"hit@10 {res['hit@10']}   ({res['n_labels']} labels)",
              f"  dedup-by-fn MRR {res['dedup_mrr']} ({res['n_fns']} fns)   "
              f"macro recall@5/pair {res['macro_recall@5']}",
              f"  presence-frontier: MRR {res['presence_mrr']}  "
              f"hit@5 {res['presence_hit@5']}  "
              f"mean-pctl {res['presence_pctl']}   ({res['n_presence']} labels)",
              f"  flip mean-pctl {res['pctl']}   (pctl: rank/of, lower=better; "
              f"hit@5 saturates when labels outnumber slots)"]
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    res = run()
    print(render(res))
