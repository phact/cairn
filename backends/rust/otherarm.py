"""
Static other-arm locator (general, source-based).

For a branch frame whose recorded outcome is one variant, find the source
line(s) where the function would have produced the OTHER variant — the path
that didn't run. This is the spec's counterfactual, drawn from the static
source (not from inference about runtime): we read the real function body and
point at the real lines that yield the not-taken arm.

Conservative by design: returns candidate lines with explicit heuristic
provenance, and `None` when the body can't be isolated or nothing matches —
never a fabricated number.
"""

import os
import re

import sources

# Not-taken arm -> regexes that produce that arm in Rust source. Patterns
# require return/arrow/combinator context so a struct field like
# `email_verified: true` is never mistaken for a `true` branch.
_SITES = {
    "err":   [r"\breturn\s+Err\b", r"\bErr\s*\(", r"\.map_err\b",
              r"\bbail!", r"\.ok_or(_else)?\b", r"\.context\b"],
    "ok":    [r"\breturn\s+Ok\b", r"=>\s*Ok\s*\("],
    "none":  [r"\breturn\s+None\b", r"=>\s*None\b", r"\bNone\s*=>", r"\.ok_or"],
    "some":  [r"\breturn\s+Some\b", r"=>\s*Some\s*\("],
    "false": [r"\breturn\s+false\b", r"=>\s*false\b"],
    "true":  [r"\breturn\s+true\b", r"=>\s*true\b"],
}

_MAX_BODY = 250   # a single fn body longer than this signals a brace miscount

# Strip string/char literals and line comments so braces inside them don't
# desync the depth counter (crude but robust enough for body delimiting).
_STR = re.compile(r'"(?:\\.|[^"\\])*"' r"|'(?:\\.|[^'\\])'" r"|//.*$")

def _clean(line):
    return _STR.sub("", line)


def _function_body(lines, decl_line):
    """Return (start_idx, end_idx) 0-based inclusive body range by brace
    matching from the declaration. None if unbalanced or implausibly large."""
    n = len(lines)
    i = decl_line - 1
    while i < n and "{" not in _clean(lines[i]):
        i += 1
        if i - (decl_line - 1) > 40:      # signature shouldn't span 40 lines
            return None
    if i >= n:
        return None
    depth = 0
    start = i
    for j in range(i, n):
        for ch in _clean(lines[j]):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return None if (j - start) > _MAX_BODY else (start, j)
    return None


def other_arm(file, decl_line, not_taken_arm):
    """file: trace-relative source path; decl_line: function decl line;
    not_taken_arm in {ok,err,some,none,true,false}. Returns a dict with
    target_line + candidates + provenance, or None."""
    pats = _SITES.get(not_taken_arm)
    if not pats:
        return None
    ap = sources.resolve(file)
    if not ap:
        return None
    try:
        lines = open(ap, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return None
    rng = _function_body(lines, decl_line)
    if not rng:
        return None
    lo, hi = rng
    rx = re.compile("|".join(pats))
    hits = []
    for k in range(lo, hi + 1):
        src = lines[k]
        # skip comment-only lines
        if src.strip().startswith("//"):
            continue
        if rx.search(src):
            hits.append((k + 1, src.strip()[:80]))
    if not hits:
        return None
    return {
        "target_line": hits[0][0],
        "candidate_lines": [h[0] for h in hits[:4]],
        "snippet": hits[0][1],
        "provenance": "static source scan (heuristic): first site producing "
                      f"the not-taken `{not_taken_arm}` arm in the function body",
    }


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 3:
        print("usage: otherarm.py <src-file> <decl-line> [arm]")
        sys.exit(2)
    f, dl = sys.argv[1], int(sys.argv[2])
    arm = sys.argv[3] if len(sys.argv) > 3 else "err"
    print(json.dumps(other_arm(f, dl, arm), indent=2))
