"""
Cairn saliency ranker (Phase 1).

Reads the grounded frame stream (out/frames.jsonl + .counts) produced by
extract.py and ranks user-crate frames so the most explanatory few rise to
the top. Emits the Phase-1 acceptance artifact: a plain-text top-N list of
  function — file:line — role — deciding value (if any)

Every input row is a real recorded stop. The ranker only *orders* and
*labels* what executed; it never invents a frame.
"""

import json
import math
import os
import re
import sys

FRAMES = os.environ.get("CAIRN_OUT", "out/frames.jsonl")
TOPN   = int(os.environ.get("CAIRN_TOPN", "20"))
# Project-supplied (gitignored config / CLI), so the engine names no scenario:
# the user-code path marker and the crate symbol prefix stripped for readability.
# Env wins (set by the pipeline subprocess); else the project config, so the
# values are consistent whether rank runs as a subprocess or is imported by
# bench/tracediff.
def _proj_get(key, env):
    v = os.environ.get(env)
    if v is not None:
        return v
    try:
        import layout
        return layout.project_config().get(key, "")
    except Exception:
        return ""

_NO_VERBS = os.environ.get("CAIRN_NO_VERBS") == "1"
USER_MARK    = _proj_get("user_mark", "CAIRN_USER_MARK")
STRIP_PREFIX = _proj_get("strip_prefix", "CAIRN_STRIP_PREFIX")

# --- boundary classification (by defining-file path) -----------------------
# The capture already fenced to user files, but we keep the classifier here so
# the same logic serves the artifact builder in Phase 2.
def classify(file_path):
    if ("/crates/" in file_path and "/src/" in file_path) or \
            (USER_MARK and USER_MARK in file_path):
        return "user_crate"
    if "/.cargo/registry/" in file_path or "/rustup/" in file_path or "/rustc/" in file_path:
        return "dependency"
    if "/tests/" in file_path or "libtest" in file_path or "test::" in file_path:
        return "libtest_harness"
    return "dependency"

# --- logical-function canonicalization -------------------------------------
# Async fns and closures are compiler desugarings of one source-level
# function. Collapse `foo::{async_fn#0}` / `foo::{closure#1}` back to `foo`
# so the ranker scores the logical unit, not its state-machine shards.
_DESUGAR = re.compile(r"::\{(?:async_fn|closure|async_block|closure_env)#\d+\}")
_GENERIC = re.compile(r"<.*>")
_IMPLNUM = re.compile(r"\{impl#\d+\}::")

_TYPESEG = re.compile(r"::[A-Z][A-Za-z0-9]*::(?=[a-z_][A-Za-z0-9_]*$)")

def canonical(fn):
    fn = _GENERIC.sub("", fn)
    fn = _DESUGAR.sub("", fn)
    fn = _IMPLNUM.sub("", fn)          # {impl#3}::get -> get
    if STRIP_PREFIX:                   # crate symbol prefix -> readable lane id
        fn = fn.replace(STRIP_PREFIX, "")
    # Strip a CamelCase type segment before the leaf so an impl method
    # (`forwarder::Forwarder::dispatch`) and its async desugaring
    # (`forwarder::{impl#0}::dispatch::{async_fn#0}` -> `forwarder::dispatch`)
    # collapse to the same logical function.
    fn = _TYPESEG.sub("::", fn)
    return fn

def leaf(fn):
    return canonical(fn).split("::")[-1]

def logical_key(row):
    """The logical function name for a frame/return row. A backend may supply
    `fn_logical` already normalized (Python qualnames, etc.); otherwise we fall
    back to Rust symbol demangling. This is the one seam where name
    normalization is language-specific — the rest of the ranker is structural."""
    return row.get("fn_logical") or canonical(row["fn"])

def module_of(fn):
    parts = canonical(fn).split("::")
    return "::".join(parts[:-1]) if len(parts) > 1 else ""

# --- scoring signals -------------------------------------------------------
# NOTE: nothing below names this scenario's functions or modules. Importance
# is DISCOVERED from the recorded call structure (flow weight) plus
# language-level naming idioms that hold for any Rust codebase.

# (c)/(d) role verbs — general decision/transform vocabulary. These are
# English/Rust naming conventions, not this program's identifiers.
DECISION_VERBS = ("validate", "verify", "authorize", "authenticate", "check",
                  "decode", "encode", "extract", "peek", "find", "lookup",
                  "resolve", "inject", "strip", "dispatch", "handle",
                  "forward", "apply", "route", "sign", "parse", "split",
                  "require", "select", "match", "compute", "build")

# Framework/derive/trait-impl glue that is plumbing in ANY Rust program —
# std traits and serde/format machinery, not domain logic. No scenario names.
PLUMBING = ("clone", "serialize", "deserialize", "visit_str", "visit_map",
            "visit_seq", "fmt", "drop", "from_iter", "into_iter", "next",
            "poll", "hash", "eq", "cmp", "partial_cmp", "as_ref", "borrow",
            "deref", "index")

def humanize(fn):
    """Generic, scenario-independent role: the real symbol name made readable.
    Grounded in the recorded function identity; prose narration is the
    downstream model's job, not the backend's."""
    return leaf(fn).replace("_", " ")

# Back-compat alias; callers pass (fn, args) but role is name-derived only.
def role_for(fn, args=None):
    return humanize(fn)

def compute_fanout(rows):
    """Signal (e): for each logical function, the max number of DISTINCT
    user-crate children it called within a single activation — the fan-out
    joints that form the skeleton. Derived from real stack-depth nesting in
    the recorded stream, not inferred."""
    fanout = {}
    stack = []  # entries: [canonical_fn, depth, set_of_child_fns]
    for r in sorted(rows, key=lambda x: x["step"]):
        d = r["depth"]
        cfn = logical_key(r)
        # pop frames that have returned (same-or-deeper depth than the new one)
        while stack and stack[-1][1] >= d:
            done = stack.pop()
            fanout[done[0]] = max(fanout.get(done[0], 0), len(done[2]))
        if stack:
            stack[-1][2].add(cfn)
        stack.append([cfn, d, set()])
    while stack:
        done = stack.pop()
        fanout[done[0]] = max(fanout.get(done[0], 0), len(done[2]))
    return fanout

def compute_fanin(rows):
    """The other half of signal (e): DISTINCT callers per logical function.
    Many call sites converging on one function is a join in the flow's
    skeleton, same as one function exploding into many. Derived from real
    stack-depth nesting, not inferred."""
    parents = {}
    stack = []  # (canonical_fn, depth)
    for r in sorted(rows, key=lambda x: x["step"]):
        d = r["depth"]
        c = logical_key(r)
        while stack and stack[-1][1] >= d:
            stack.pop()
        if stack:
            parents.setdefault(c, set()).add(stack[-1][0])
        stack.append((c, d))
    return {fn: len(ps) for fn, ps in parents.items()}


def compute_contingency(rows, decision_fns):
    """Single-trace CONTINGENCY: a function is contingent if any activation on
    its live call stack had ALREADY seen a sibling produce a recorded outcome
    arm — i.e. this code ran on the far side of a decision. This is the
    single-trace predictor of what trace-diffs measure (presence divergence:
    the only-in-this-run path exists because a decision let it run)."""
    contingent = set()
    stack = []  # [fn, depth, seen_decision_child]
    for r in sorted(rows, key=lambda x: x["step"]):
        d, c = r["depth"], logical_key(r)
        while stack and stack[-1][1] >= d:
            stack.pop()
        if any(s[2] for s in stack):       # downstream of a decision anywhere
            contingent.add(c)
        if stack and c in decision_fns:
            stack[-1][2] = True            # later siblings are post-decision
        stack.append([c, d, False])
    return contingent


def compute_flow_weight(rows):
    """Discovered 'center of gravity', replacing any hardcoded module table.

    The main flow is the dominant call subtree; setup/teardown are shallow
    siblings of it. For each function we take the largest subtree it
    participates in — its OWN transitive descendant count, or that of any
    ancestor it sits under — and normalize by the biggest subtree in the run.
    Request-flow frames (deep under the dominant joint) approach 1.0; fixture
    setup (small isolated subtrees) stays near 0. Pure structure, no names."""
    rows = sorted(rows, key=lambda x: x["step"])
    subtree = {}                       # canonical_fn -> max transitive descendants
    # entries: [canon, depth, descendants:set, ancestors:tuple]
    stack = []
    anc_max = {}                       # canonical_fn -> max ancestor subtree seen
    # First pass needs subtree sizes; compute by closing frames and bubbling
    # descendant sets upward, while recording each frame's ancestor chain.
    anc_chain = {}                     # canonical_fn -> set of ancestor canon fns (union)
    for r in rows:
        d = r["depth"]
        c = logical_key(r)
        while stack and stack[-1][1] >= d:
            done = stack.pop()
            subtree[done[0]] = max(subtree.get(done[0], 0), len(done[2]))
            if stack:
                stack[-1][2].update(done[2])
                stack[-1][2].add(done[0])
        anc_chain.setdefault(c, set()).update(s[0] for s in stack)
        stack.append([c, d, set()])
    while stack:
        done = stack.pop()
        subtree[done[0]] = max(subtree.get(done[0], 0), len(done[2]))
        if stack:
            stack[-1][2].update(done[2])
            stack[-1][2].add(done[0])
    gmax = max(subtree.values(), default=1) or 1
    weight = {}
    for c in subtree:
        own = subtree.get(c, 0)
        anc = max((subtree.get(a, 0) for a in anc_chain.get(c, ())), default=0)
        weight[c] = max(own, anc) / gmax
    return weight

def score(fn, count, mindepth, depth_lo, depth_hi, fanout, flow, fanin,
          contingent=None):
    lf = leaf(fn)
    # (a) discovered flow weight is the base prior (0..1): how central this
    # function is to the dominant recorded flow vs. peripheral setup.
    s = 0.25 + 0.75 * flow.get(fn, 0.0)

    # (e) fan-out/fan-in joint: a call that explodes into many children is the
    # skeleton of the flow. Strong boost, saturating in log space.
    children = fanout.get(fn, 0)
    if children > 0:
        s += 0.45 * min(1.0, math.log(1 + children) / math.log(8))
    # ...and convergence: many DISTINCT call sites funneling into one function
    # is a join. A single caller is nothing special, so boost only >= 2.
    parents = fanin.get(fn, 0)
    if parents >= 2:
        s += 0.25 * min(1.0, math.log(parents) / math.log(6))


    # (c)/(d) decision/transform verbs boost; plumbing sinks.
    # CAIRN_NO_VERBS ablates these hand-curated lexical heuristics so bench can
    # measure how much of the score was structure vs. crate-flattering vocab.
    if not _NO_VERBS:
        if any(lf == p or lf.startswith(p) for p in PLUMBING):
            s -= 0.45
        if any(v in lf for v in DECISION_VERBS):
            s += 0.28

    # test seams (install_for_test, *_mock) are scaffolding, not the program.
    if "for_test" in lf or lf.endswith("_test") or "mock" in lf:
        s -= 0.6

    # (f) frequency: hit-once = decision point (boost). Only *clearly* hot
    # frames (real loops, not closures merged into a parent) get sunk.
    if count == 1:
        s += 0.10
    elif count >= 8:
        s -= 0.05 * math.log(count)

    # spine: shallower stack depth = closer to the request entry.
    if depth_hi > depth_lo:
        s += 0.30 * (1 - (mindepth - depth_lo) / (depth_hi - depth_lo))

    return s

# --- deciding values (from captured return values) -------------------------
_VARIANT = re.compile(r"::(Ok|Err|Some)\((.*)\)\s*$")
_RESOPT  = re.compile(r"(Result|Option)<\s*([^,>]+)")

def concise_return(v):
    """Reduce a raw DWARF return rendering to a short deciding value, or
    None if it isn't a clean decision (Poll/Unresumed/struct/pointer).
    Values are truncated upstream, so the variant is detected from the
    prefix (`::Ok(`, `::Some(`, `::None`), never the closing paren."""
    if not v or v in ("<unit>", "<unreadable>"):
        return None
    if v in ("true", "false") or v.startswith('"') or re.match(r"^-?\d+$", v):
        return v
    vm = re.search(r"::(Ok|Err|Some)\(", v)
    if vm:
        variant = vm.group(1)
        # Only Ok/Some carry the FIRST generic as their payload type. Err's
        # payload is the error type (2nd param); naming the Ok type there
        # would be wrong, so we show the bare variant rather than mislabel.
        tyname = ""
        if variant in ("Ok", "Some"):
            tm = _RESOPT.search(v)
            if tm:
                tyname = tm.group(2).strip().split("::")[-1]
                if not re.match(r"^[A-Za-z_]", tyname):   # tuples, refs, etc.
                    tyname = ""
        return f"{variant}({tyname})" if tyname else variant
    if re.search(r"::None\b", v) or v == "None":
        return "None"
    return None  # Poll<>, async_fn_env, raw structs/pointers aren't decisions

def deciding_value(values):
    """Pick the most informative clean return across a fn's activations."""
    best = None
    for v in values:
        c = concise_return(v)
        if c is None:
            continue
        # prefer variants/strings over bare bools when both exist
        rank_key = (0 if c in ("true", "false") else 1, len(c))
        if best is None or rank_key > best[0]:
            best = (rank_key, c)
    return best[1] if best else None

def load_returns(frames_path):
    try:
        raw = json.load(open(frames_path + ".returns"))
    except FileNotFoundError:
        return {}
    by_fn = {}
    for r in raw:
        # Prefer the backend's outcome classification (Python, future backends);
        # fall back to parsing the raw Rust DWARF return string.
        if r.get("outcome"):
            disp = r["outcome"].get("display")
        else:
            disp = concise_return(r["value"])
        if disp:
            by_fn.setdefault(logical_key(r), []).append(disp)
    return {fn: _pick_display(vs) for fn, vs in by_fn.items() if vs}


def _pick_display(displays):
    """Most informative deciding value across a fn's activations."""
    best = None
    for c in displays:
        key = (0 if c in ("true", "false", "True", "False") else 1, len(c))
        if best is None or key > best[0]:
            best = (key, c)
    return best[1] if best else None


def analyze(frames_path):
    """Single source of ranking truth. Returns:
      agg     : canonical_fn -> aggregate {fn,file,line,mindepth,first,count,
                args,fanout,salience,salience_norm}
      ranked  : agg values sorted by salience
      decisions: canonical_fn -> deciding value (str | None)
    Both the CLI and the artifact builder consume this so a tier can never
    disagree with the top-20."""
    rows = [json.loads(l) for l in open(frames_path)]
    counts = json.load(open(frames_path + ".counts"))
    decisions = load_returns(frames_path)

    agg = {}
    for r in rows:
        c = logical_key(r)
        a = agg.setdefault(c, {
            "fn": c, "file": r["file"], "line": r["line"],
            "mindepth": r["depth"], "first": r["step"], "count": 0,
            "args": r.get("args", []), "raw_names": set(),
        })
        a["mindepth"] = min(a["mindepth"], r["depth"])
        a["first"] = min(a["first"], r["step"])
        a["raw_names"].add(r["fn"])
        if "{async_fn" not in r["fn"] and "{closure" not in r["fn"]:
            a["file"], a["line"] = r["file"], r["line"]
            if r.get("args"):
                a["args"] = r["args"]
    for c, a in agg.items():
        a["count"] = sum(counts.get(n, 0) for n in a["raw_names"]) or 1
        a["raw_names"] = sorted(a["raw_names"])

    fanout = compute_fanout(rows)
    fanin = compute_fanin(rows)
    flow = compute_flow_weight(rows)
    contingent = compute_contingency(rows, set(decisions))
    # (b) author-intent: functions carrying tracing spans/events. A Rust
    # source scan — gated on the DECLARED language, not try/except silence.
    annotated = set()
    if os.environ.get("CAIRN_LANGUAGE", "rust") == "rust":
        try:
            import sys as _sys
            _rd = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "backends", "rust")
            if _rd not in _sys.path:
                _sys.path.append(_rd)
            import spans
            sig = spans.detect({r["file"] for r in rows})
            annotated = sig["event_fns"] | sig["span_fns"]
        except Exception:
            pass

    depths = [a["mindepth"] for a in agg.values()]
    depth_lo, depth_hi = min(depths), max(depths)
    for a in agg.values():
        a["fanout"] = fanout.get(a["fn"], 0)
        a["fanin"] = fanin.get(a["fn"], 0)
        a["flow"] = round(flow.get(a["fn"], 0.0), 3)
        a["annotated"] = (a["file"], leaf(a["fn"])) in annotated
        a["contingent"] = a["fn"] in contingent
        a["salience"] = score(a["fn"], a["count"], a["mindepth"],
                              depth_lo, depth_hi, fanout, flow, fanin,
                              contingent)
        if a["annotated"]:
            a["salience"] += 0.12      # signal (b): the author marked it
    a_count = sum(1 for a in agg.values() if a["annotated"])

    ranked = sorted(agg.values(), key=lambda a: (-a["salience"], a["first"]))
    hi = max(a["salience"] for a in ranked)
    lo = min(a["salience"] for a in ranked)
    for a in ranked:
        a["salience_norm"] = 0.0 if hi == lo else round((a["salience"] - lo) / (hi - lo), 2)
    return agg, ranked, decisions, rows


def main():
    agg, ranked, decisions, rows = analyze(FRAMES)
    def norm(x):
        return x  # already normalized in analyze

    print(f"\n=== Cairn Phase 1 — top {TOPN} salient frames "
          f"({os.environ.get('CAIRN_SCENARIO_ID', 'trace')}) ===\n")
    print("  #  sal    ×   function — role  [file:line]  ⟶ deciding value")
    print("-" * 100)
    for i, a in enumerate(ranked[:TOPN], 1):
        n = a["salience_norm"]
        role = role_for(a["fn"], a["args"])
        xn = f"×{a['count']}" if a["count"] > 1 else ""
        dv = decisions.get(a["fn"])
        dv_s = f"   ⟶ {dv}" if dv else ""
        print(f"{i:>3} {n:>4} {xn:>4}   {a['fn']} — {role}  "
              f"[{a['file'].split('/')[-1]}:{a['line']}]{dv_s}")

    if "--full" in sys.argv:
        print("\n--- full ranking (for tuning) ---")
        for a in ranked:
            print(f"{a['salience_norm']:>4} ×{a['count']:<3} d{a['mindepth']:<3} "
                  f"{a['fn']}  [{a['file'].split('/')[-1]}:{a['line']}]")

if __name__ == "__main__":
    main()
