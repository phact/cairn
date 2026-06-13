"""
General tracing-signal detector (no scenario-specific names).

Scans the user-crate source for two author-intent markers:
  * SPAN boundaries  — `#[instrument]` / `#[tracing::instrument]` and the
    `*_span!` macros. These ARE the logical span tree the spec wants causal
    edges built from.
  * EVENT annotations — `info!/debug!/warn!/error!/trace!` (and `tracing::*!`).
    Not spans, but the author still marked the spot important: the spec's
    saliency signal (b).

Each marker is mapped to its enclosing function by source position. Works on
any Rust crate; finds nothing gracefully (then the artifact honestly keeps
call-nesting causality and says so).
"""

import os
import re

from sources import resolve as _resolve  # core path resolution

_FN = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
                 r"(?:extern\s+\"[^\"]*\"\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)")
_INSTRUMENT = re.compile(r"#\[\s*(?:tracing::)?instrument")
_SPAN_MAC = re.compile(r"\b(?:info|debug|trace|warn|error)_span!\s*\(|\bspan!\s*\(")
_EVENT_MAC = re.compile(r"(?:\btracing::)?\b(?:info|debug|warn|error|trace)!\s*\(")




def _scan_file(path):
    """Return {fn_leaf: {'span': bool, 'event': bool}} for one source file."""
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return {}
    # function decl line numbers (1-based) -> leaf name, in order
    decls = []
    for i, ln in enumerate(lines, 1):
        m = _FN.match(ln)
        if m:
            decls.append((i, m.group(1)))

    def enclosing(line_no):
        name = None
        for dl, nm in decls:
            if dl <= line_no:
                name = nm
            else:
                break
        return name

    out = {}
    pending_instrument = False
    for i, ln in enumerate(lines, 1):
        if _INSTRUMENT.search(ln):
            pending_instrument = True
            continue
        m = _FN.match(ln)
        if m and pending_instrument:
            out.setdefault(m.group(1), {"span": False, "event": False})["span"] = True
            pending_instrument = False
            continue
        if m:
            pending_instrument = False
        if _SPAN_MAC.search(ln):
            fn = enclosing(i)
            if fn:
                out.setdefault(fn, {"span": False, "event": False})["span"] = True
        elif _EVENT_MAC.search(ln):
            fn = enclosing(i)
            if fn:
                out.setdefault(fn, {"span": False, "event": False})["event"] = True
    return out


def detect(files):
    """files: iterable of source paths as they appear in the trace (relative
    to the crate root, or absolute). Returns dicts keyed by (file_rel, leaf):
      span_fns, event_fns  -> sets of (file, leaf)
      any_span             -> bool (was a real span found anywhere)
    """
    span_fns, event_fns = set(), set()
    for f in set(files):
        if f in ("?", "", None):
            continue
        ap = _resolve(f)
        if not ap:
            continue
        for leaf, sig in _scan_file(ap).items():
            if sig["span"]:
                span_fns.add((f, leaf))
            if sig["event"]:
                event_fns.add((f, leaf))
    return {"span_fns": span_fns, "event_fns": event_fns,
            "any_span": bool(span_fns)}


if __name__ == "__main__":
    import json
    import sys
    files = sys.argv[1:]
    if not files:
        print("usage: spans.py <src-file> [<src-file> ...]")
        sys.exit(2)
    d = detect(files)
    print("any_span:", d["any_span"])
    print("span_fns:", sorted(d["span_fns"]))
    print("event_fns:", sorted(f"{os.path.basename(a)}::{b}" for a, b in d["event_fns"]))
