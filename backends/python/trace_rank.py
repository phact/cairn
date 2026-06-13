"""
Dogfood driver: run Cairn's OWN ranker over the real Rust frame stream, under
the Python capture backend — Cairn tracing Cairn.

The user-source scope is cairn/ (so rank.py + layout.py internals are the
recorded program); this driver file sits outside the scope, like a test
harness, and is fenced out of the trace.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "cairn"))

import layout                                              # noqa: E402
import rank                                                # noqa: E402


def main():
    # the first trace that has an rr recording = the Rust scenario
    rust = [t for t in layout.list_traces() if layout.rr_trace(t)]
    if not rust:
        raise SystemExit("no rust trace to analyze")
    frames = layout.frames_path(rust[0])
    agg, ranked, decisions, rows = rank.analyze(frames)
    top = [(a["fn"], a["salience_norm"]) for a in ranked[:5]]
    print("analyzed", len(rows), "rows ->", len(ranked), "ranked; top5:", top)


main()
