#!/usr/bin/env python3
"""
Cairn CLI — the terminal interface to the Cairn engine.

The CLI is the substrate (a future MCP server only wraps it). Every read command
returns RECORDED GROUND TRUTH (or static counterfactuals explicitly labeled so);
it never reasons, never calls a model, never guesses what probably ran.

Read commands take `--json` (output reuses the trace-artifact schema the REST API
and UI share); default output is human-readable. The active trace lives in
.cairn/config.toml; `--trace <id>` overrides it; every command echoes the
resolved trace id. Tier-3 (loc/step/value) is served by the local warm session
over HTTP; artifact projections are read straight from disk.

Exit codes: 0 ok · 2 no active trace · 3 frame/trace not found · 4 recording failed.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layout  # noqa: E402

SERVER = os.environ.get("CAIRN_SERVER", "http://127.0.0.1:8787")

EXIT_OK, EXIT_NO_TRACE, EXIT_NOT_FOUND, EXIT_RECORD_FAILED = 0, 2, 3, 4


class CairnError(Exception):
    def __init__(self, msg, code=EXIT_NOT_FOUND):
        super().__init__(msg)
        self.code = code


# ---- active trace + artifact (one layout, no fallback) ---------------------
def resolve(args):
    """Resolve (trace_id, artifact). Raises CairnError(2) if no active trace,
    (3) if the trace has no artifact."""
    tid = getattr(args, "trace", None) or getattr(args, "_gtrace", None) \
        or layout.resolve_active()
    if not tid:
        raise CairnError("no active trace — run `cairn record` or `cairn use <id>`",
                         EXIT_NO_TRACE)
    ap = layout.artifact_path(tid)
    if not os.path.exists(ap):
        raise CairnError(f"no such trace `{tid}` (try `cairn traces`)", EXIT_NOT_FOUND)
    try:
        art = json.load(open(ap))
    except (OSError, json.JSONDecodeError) as e:
        raise CairnError(f"artifact for `{tid}` unreadable: {e}")
    return tid, art


# ---- HTTP to the warm-session server (tier 3) ------------------------------
def _http(method, path, body=None, timeout=200):
    url = SERVER + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise CairnError(f"server {e.code}: {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        raise CairnError(f"cannot reach Cairn server at {SERVER} ({e.reason}); "
                         f"is `cairn watch` running?")


# The salient path `<n>` indexes — shared by skeleton/frame/state/explain so the
# numbers always line up ("skeleton 5" and "explain 5" are the same frame).
DEFAULT_THRESHOLD = 0.6


# ---- salient-frame indexing (<n> is the path position) ---------------------
def salient_frames(art, threshold=DEFAULT_THRESHOLD):
    """One representative frame per lane above the salience threshold, ordered
    by execution step — the path the user walks. `<n>` is 1-based into this."""
    by_lane = {}
    for f in art["frames"]:
        if f["salience"] < threshold:
            continue
        cur = by_lane.get(f["lane"])
        if cur is None or f["salience"] > cur["salience"]:
            by_lane[f["lane"]] = f
    return sorted(by_lane.values(), key=lambda f: f["step"])


def nth_frame(art, n, threshold=DEFAULT_THRESHOLD):
    frames = salient_frames(art, threshold)
    if not (1 <= n <= len(frames)):
        raise CairnError(f"frame {n} out of range (1..{len(frames)})")
    return frames[n - 1], len(frames)


# ---- decoders shared with the UI are applied server-side; the artifact
#      already carries decoded values + deref.status. The CLI only projects. --
def loc_for(tid, fid, instant=None):
    sc = urllib.parse.quote(tid, safe=":")
    if instant is None:
        return _http("GET", f"/scenario/{sc}/frame/{fid}/loc")
    return _http("GET", f"/scenario/{sc}/frame/{fid}/loc/step/{instant}")


def _add_expand_commands(values, n):
    """Stamp each unexpanded pointer node with its ready-to-run resume handle
    (`deref.expand`), per the spec: the agent keys on deref.status and runs
    exactly this command to follow the pointer at the right instant."""
    def walk(node):
        d = node.get("deref")
        if d and d.get("status") == "unexpanded" and d.get("path"):
            inst = f" --instant {d['instant']}" if d.get("instant") is not None else ""
            d["expand"] = (f"cairn value {n} --path '{json.dumps(d['path'])}'"
                           f"{inst} --depth 4")
        for c in node.get("children", []):
            walk(c)
    for v in values:
        if v.get("tree"):
            walk(v["tree"])
    return values


def recorded_state(tid, frame):
    """The RECORDED STATE panel data: the richest recorded locals at this frame,
    decoders + deref.status already applied by the engine. Falls back to the
    artifact frame values if tier-3 (loc_state) is unavailable for this backend."""
    try:
        loc = loc_for(tid, frame["id"])
        best = max(loc.get("lines", []), key=lambda l: len(l.get("locals", [])),
                   default=None)
        if best:
            return best["locals"], loc.get("reverse_step")
    except CairnError:
        pass
    return frame.get("values", []), None


# ============================ commands ======================================
def out(args, human, payload):
    if getattr(args, "json", False) or getattr(args, "_gjson", False):
        print(json.dumps(payload, indent=2))
    else:
        print(human)


def cmd_status(args):
    tid, art = resolve(args)
    sc = art["scenario"]
    n = len(salient_frames(art))
    payload = {"trace": tid, "frame_count": len(art["frames"]),
               "salient_frames": n, "capture_backend": sc.get("capture_backend"),
               "language": sc.get("language"), "capabilities": art.get("capabilities")}
    human = (f"active trace : {tid}\n"
             f"frames       : {len(art['frames'])} ({n} salient)\n"
             f"backend      : {sc.get('capture_backend')} ({sc.get('language')})")
    out(args, human, payload)


def cmd_use(args):
    tid = args.trace_id
    if not os.path.exists(layout.artifact_path(tid)):
        raise CairnError(f"no such trace `{tid}` (try `cairn traces`)")
    layout.write_active(tid)
    # Re-warm the server's tier-3 session for this trace (grounding guard: a
    # stale session would serve state from the previous recording).
    warmed = None
    try:
        warmed = _http("POST", "/session/use", {"trace": tid}).get("trace")
    except CairnError:
        pass  # server not running; config is the source of truth either way
    out(args, f"active trace -> {tid}" + (f" (session re-warmed)" if warmed else ""),
        {"trace": tid, "active": True, "session_warmed": warmed == tid})


def cmd_traces(args):
    ids = layout.list_traces()
    active = layout.resolve_active()
    payload = {"trace": active, "traces": ids, "active": active}
    human = "\n".join((("* " if t == active else "  ") + t) for t in ids) or "(none)"
    out(args, human, payload)


def cmd_precis(args):
    tid, art = resolve(args)
    payload = {"trace": tid, "precis": art["precis"], "boundaries": art["boundaries"]}
    human = art["precis"] + "\n\nI/O boundaries:\n" + "\n".join(
        f"  {b['detail']}  ({b['lane']})" for b in art["boundaries"])
    out(args, human, payload)


def cmd_skeleton(args):
    tid, art = resolve(args)
    frames = salient_frames(art, args.threshold)
    rows = [{"n": i, "lane": f["lane"], "file": f["file"], "line": f["line"],
             "kind": f["kind"], "salience": f["salience"], "role": f.get("role")}
            for i, f in enumerate(frames, 1)]
    keep = {f["lane"] for f in frames}
    lane_of = {f["id"]: f["lane"] for f in art["frames"]}
    seen, edges = set(), []
    for e in art["causal_edges"]:
        a, b = lane_of.get(e["from"]), lane_of.get(e["to"])
        if a in keep and b in keep and a != b and (a, b, e["relation"]) not in seen:
            seen.add((a, b, e["relation"]))
            edges.append({"from": a, "to": b, "relation": e["relation"]})
    payload = {"trace": tid, "threshold": args.threshold, "frames": rows,
               "causal_edges": edges}
    human = "\n".join(f"{r['n']:>3}  {r['salience']:.2f}  {r['lane']}  "
                      f"[{os.path.basename(r['file'])}:{r['line']}]  {r['kind']}"
                      for r in rows)
    out(args, human, payload)


def cmd_frame(args):
    tid, art = resolve(args)
    frame, total = nth_frame(art, args.n)
    payload = {"trace": tid, "frame": args.n, "of": total, **frame}
    human = (f"frame {args.n}/{total}  {frame['lane']}  "
             f"[{os.path.basename(frame['file'])}:{frame['line']}]  {frame['kind']}\n"
             f"role: {frame.get('role')}\n" + _fmt_values(frame.get("values", [])))
    if frame.get("branch"):
        human += "\n" + _fmt_branch(frame["branch"])
    out(args, human, payload)


def cmd_state(args):
    tid, art = resolve(args)
    frame, total = nth_frame(art, args.n)
    state, reverse = recorded_state(tid, frame); _add_expand_commands(state, args.n)
    payload = {"trace": tid, "frame": args.n, "location": _loc_str(frame),
               "recorded_state": state, "reverse_step": reverse}
    human = f"recorded state @ frame {args.n}  {_loc_str(frame)}\n" + _fmt_values(state)
    out(args, human, payload)


def cmd_loc(args):
    tid, art = resolve(args)
    frame, _ = nth_frame(art, args.n)
    res = loc_for(tid, frame["id"])
    res["trace"] = tid
    if getattr(args,"json",False) or getattr(args,"_gjson",False):
        print(json.dumps(res, indent=2))
    else:
        for ln in res.get("lines", []):
            # display-side ellipsis only — full values live in --json
            locs = ", ".join(f"{v['name']}={_short(v)[:60]}"
                             for v in ln.get("locals", [])[:4])
            print(f"  {ln['line']:>4}  {(ln.get('text') or '').strip():<48}  {locs}")
        rs = res.get("reverse_step", {})
        if rs.get("matches_forward"):
            print(f"  ↻ reverse-step re-read line {rs['reread_line']} · matches forward")


def cmd_step(args):
    tid, art = resolve(args)
    frame, _ = nth_frame(art, args.n)
    # --back re-reads earlier state; instant defaults to one before the last line
    res = loc_for(tid, frame["id"], instant=args.instant)
    rs = res.get("reverse_step", {})
    payload = {"trace": tid, "frame": args.n, "reverse_step": rs}
    human = (f"reverse-step @ frame {args.n}: re-read line {rs.get('reread_line')} "
             f"(matches forward: {rs.get('matches_forward')})")
    out(args, human, payload)


def cmd_value(args):
    tid, art = resolve(args)
    frame, _ = nth_frame(art, args.n)
    sc = urllib.parse.quote(tid, safe=":")
    q = {"path": json.dumps(args.path), "depth": str(args.depth)}
    if args.instant is not None:
        q["instant"] = str(args.instant)
    res = _http("GET", f"/scenario/{sc}/frame/{frame['id']}/value?" +
                urllib.parse.urlencode(q))
    res["trace"] = tid
    out(args, _fmt_tree(res.get("tree", {})), res)


def cmd_sources(args):
    tid, art = resolve(args)
    sc = urllib.parse.quote(tid, safe=":")
    res = _http("GET", f"/scenario/{sc}/sources")
    res["trace"] = tid
    human = "\n".join(f"  {f['path']}  ({f['frame_count']} frames)"
                      for f in res.get("files", []))
    out(args, human, res)


def cmd_source(args):
    tid, art = resolve(args)
    sc = urllib.parse.quote(tid, safe=":")
    res = _http("GET", f"/scenario/{sc}/source?path=" +
                urllib.parse.quote(args.file))
    res["trace"] = tid
    if getattr(args,"json",False) or getattr(args,"_gjson",False):
        print(json.dumps(res, indent=2))
    else:
        ex = set(res.get("executed_lines", []))
        for ln in res.get("lines", []):
            mark = "›" if ln["n"] in ex else " "
            print(f"{mark}{ln['n']:>5}  {ln['text']}")


# ---- the explain question: pointer (agent) vs bundle (unprimed) ------------
FOLLOW_UP = [
    "cairn state {n} --json        # full recorded state at this frame",
    "cairn loc {n} --json          # line-by-line execution with values",
    "cairn step {n} --back         # reverse-step to re-read earlier state",
    "cairn frame {p} --json        # the prior salient frame",
    "cairn source --file {file}    # source (you likely already have this)",
]
GROUNDING = ("Explain only from recorded values above. `not_taken` is the static "
             "alternative arm — describe it with 'would', not 'did'. If you need a "
             "value not present here, run a follow_up command; do not guess.")


def cmd_explain(args):
    """Pointer form, for an agent that has the CLI + Skill. Hands over THIS
    frame's recorded state (the part the agent can't get itself) + follow-up
    commands. No prose, no source."""
    tid, art = resolve(args)
    frame, total = nth_frame(art, args.n)
    state, _ = recorded_state(tid, frame); _add_expand_commands(state, args.n)
    rs = {v["name"]: {k: v[k] for k in v if k != "name"} for v in state}
    payload = {
        "trace": tid, "frame": args.n, "location": _loc_str(frame),
        "kind": frame["kind"], "role": frame.get("role"),
        "branch": frame.get("branch"),
        "recorded_state": rs,
        "question": args.question or None,
        "follow_up": [s.format(n=args.n, p=max(1, args.n - 1),
                               file=os.path.basename(frame["file"]))
                      for s in FOLLOW_UP],
        "grounding": GROUNDING,
    }
    if getattr(args,"json",False) or getattr(args,"_gjson",False):
        print(json.dumps(payload, indent=2))
    else:
        print(f"frame {args.n}/{total}  {payload['location']}  {frame['kind']}")
        print(f"role: {frame.get('role')}")
        if frame.get("branch"):
            print(_fmt_branch(frame["branch"]))
        print("recorded state (the part only Cairn has):")
        print(_fmt_values(state))
        print("\nfollow up:")
        for s in payload["follow_up"]:
            print("  " + s)
        print("\n" + GROUNDING)


def cmd_handoff(args):
    """Bundle form, for an unprimed receiver (clipboard / deep-link). Self-
    contained text incl. a source slice — the ONLY command that includes source."""
    tid, art = resolve(args)
    frame, total = nth_frame(art, args.n)
    state, _ = recorded_state(tid, frame)
    # source slice around the line
    slice_txt = ""
    try:
        sc = urllib.parse.quote(tid, safe=":")
        rel = "/".join(frame["file"].split("/")[-2:])
        src = _http("GET", f"/scenario/{sc}/source?path=" + urllib.parse.quote(rel))
        lo = max(1, frame["line"] - 4)
        hi = frame["line"] + 4
        slice_txt = "\n".join(f"{l['n']:>4}: {l['text']}" for l in src["lines"]
                              if lo <= l["n"] <= hi)
    except CairnError:
        slice_txt = "(source unavailable)"
    lines = [
        f"You are explaining a REAL recorded execution (rr) of `{tid}`.",
        f"Frame {args.n}/{total}: {_loc_str(frame)} ({frame['kind']}). "
        f"Role: {frame.get('role')}.", "",
        "Source:", slice_txt, "",
        "Recorded state (real values from the run):",
    ]
    for v in state:
        lines.append(f"  {v['name']} = {_short(v)}")
    if frame.get("branch"):
        b = frame["branch"]
        lines += ["", f"Branch: took `{b.get('taken_arm')}` "
                  f"(deciding: {b.get('deciding_value')}).",
                  f"Not taken (STATIC alternative — describe with 'would'): "
                  f"{(b.get('not_taken') or {}).get('summary')}"]
    if args.question:
        lines += ["", f"Question: {args.question}"]
    lines += ["", "Explain ONLY from the recorded values above. The not-taken arm "
              "is static — say 'would', never 'did'. Don't guess values not shown."]
    bundle = "\n".join(lines)
    out(args, bundle, {"trace": tid, "frame": args.n, "bundle": bundle})


def cmd_predict_log(args):
    tid, _ = resolve(args)
    sc = urllib.parse.quote(tid, safe=":")
    body = {"frame_id": args.frame, "guessed": args.guessed, "truth": args.truth,
            "correct": args.correct.lower() in ("true", "1", "yes")}
    res = _http("POST", f"/scenario/{sc}/predictions", body)
    out(args, f"logged: {res.get('logged')}", {"trace": tid, **res})


def cmd_record(args):
    import pipeline
    quiet = (lambda *a: None) if (getattr(args, "json", False) or
                                  getattr(args, "_gjson", False)) else print
    try:
        if args.lang == "python" or args.script:
            if not args.script:
                raise CairnError("--lang python needs --script <entry.py>",
                                 EXIT_RECORD_FAILED)
            tid = pipeline.record_python(args.script, args.src, log=quiet)
        else:
            if not (args.crate and args.test):
                raise CairnError("rust recording needs --crate and --test",
                                 EXIT_RECORD_FAILED)
            tid = pipeline.record(args.crate, args.test, log=quiet)
    except pipeline.PipelineError as e:
        raise CairnError(f"recording failed: {e}", EXIT_RECORD_FAILED)
    out(args, f"recorded -> {tid}", {"trace": tid, "scenario_id": tid})


def cmd_derive(args):
    """Rebuild derived/ from the existing recording (regenerability check)."""
    import pipeline
    tid = getattr(args, "trace", None) or getattr(args, "_gtrace", None) \
        or layout.resolve_active()
    if not tid:
        raise CairnError("no active trace", EXIT_NO_TRACE)
    quiet = (lambda *a: None) if (getattr(args, "json", False) or
                                  getattr(args, "_gjson", False)) else print
    try:
        pipeline.derive(tid, log=quiet)
    except pipeline.PipelineError as e:
        raise CairnError(str(e), EXIT_RECORD_FAILED)
    out(args, f"re-derived -> {tid}", {"trace": tid, "derived": True})


def cmd_capture_on_fail(args):
    """Wrap a test command: transparent while green, record each failing test
    under rr while red. Preserves the wrapped command's exit code."""
    import capturefail
    cmd = list(args.command)
    if cmd and cmd[0] == "--":          # argparse may keep the separator
        cmd = cmd[1:]
    if not cmd:
        raise CairnError("usage: cairn capture-on-fail -- <test command>",
                         EXIT_RECORD_FAILED)
    code = capturefail.capture_on_fail(cmd, cap=args.cap,
                                       user_mark=args.user_mark, files=args.files)
    # the wrapper IS the test runner — exit with the runner's code so CI is honest
    sys.exit(code)


def cmd_capture_fix(args):
    """Record the green side of a captured failure after the fix lands; emits
    the `cairn diff` that yields the bug's flip signature."""
    import capturefail
    try:
        tid = capturefail.capture_fix(args.red, binary=args.bin)
    except Exception as e:
        raise CairnError(str(e), EXIT_RECORD_FAILED)
    out(args, f"captured fix -> {tid}", {"trace": tid, "red": args.red})


def cmd_diff(args):
    """Diff two recordings: divergence frontier + benchmark-v0 ranker check."""
    import tracediff
    base = getattr(args, "trace", None) or getattr(args, "_gtrace", None) \
        or layout.resolve_active()
    if not base:
        raise CairnError("no base trace (use --trace)", EXIT_NO_TRACE)
    try:
        d = tracediff.diff(base, args.other)
    except tracediff.DiffError as e:
        raise CairnError(str(e))
    d["trace"] = base
    out(args, tracediff.render(d), d)


def cmd_bench(args):
    """Ranker benchmark: deciding-frame ranks over all trace pairs."""
    import bench
    res = bench.run()
    res["trace"] = layout.resolve_active()
    out(args, bench.render(res), res)


def cmd_open(args):
    tid = layout.resolve_active()
    frag = f"#frame={args.n}" if args.n else ""
    url = f"{SERVER}/?trace={urllib.parse.quote(tid or '')}{frag}"
    # launch the browser when possible; always print the URL
    import shutil as _shutil
    import subprocess as _sp
    if _shutil.which("xdg-open"):
        _sp.Popen(["xdg-open", url], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    out(args, url, {"trace": tid, "url": url})


def cmd_watch(args):
    """The local server: tiered query API + browser workspace, foreground."""
    print(f"cairn watch — serving {SERVER}  (ctrl-c to stop)")
    os.execvp(sys.executable,
              [sys.executable, os.path.join(ROOT, "cairn", "api.py")])


# ---- formatting helpers ----------------------------------------------------
def _loc_str(frame):
    return f"{'/'.join(frame['file'].split('/')[-2:])}:{frame['line']}"


def _short(v):
    s = v.get("value", "")
    extra = ""
    if v.get("as_time"):
        extra = f"  ⏱{v['as_time']}"
    d = v.get("deref")
    if d and d.get("status") == "unexpanded":
        extra = "  «unexpanded ptr — cairn value to follow»"
    return f"{s}{extra}"


def _fmt_values(values):
    # human display ellipsizes long values; the full bytes are in --json
    def disp(v):
        s = _short(v)
        return s if len(s) <= 240 else s[:240] + " …(--json for full value)"
    return "\n".join(f"  {v['name']} = {disp(v)}" for v in values) or "  (none)"


def _fmt_branch(b):
    nt = b.get("not_taken") or {}
    return (f"branch: took `{b.get('taken_arm')}` (deciding: {b.get('deciding_value')})\n"
            f"  not taken [static]: `{nt.get('arm')}` — {nt.get('summary')}"
            + (f" (line {nt['target_line']})" if nt.get("target_line") else ""))


def _fmt_tree(node, ind=0, name=None):
    tag = (name + ": ") if name else ""
    extra = ""
    d = node.get("deref")
    if d:
        extra = f"  [{d['status']}]"
    if node.get("as_time"):
        extra += f"  ⏱{node['as_time']}"
    line = "  " * ind + tag + str(node.get("summary") or node.get("type") or "?") + extra
    out_lines = [line]
    for c in node.get("children", [])[:12]:
        out_lines.append(_fmt_tree(c, ind + 1, c.get("name")))
    return "\n".join(out_lines)


# ============================ argparse ======================================
def build_parser():
    p = argparse.ArgumentParser(prog="cairn", description="Recorded-execution truth at the terminal.")
    # --json / --trace are accepted both before AND after the subcommand (a
    # shared parent), so `cairn state 5 --json` and `cairn --json state 5` both work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--trace", help="override the active trace id")
    common.add_argument("--json", action="store_true", help="machine output (artifact schema)")
    # Global copies use distinct dests so a subcommand's `--trace` default (None)
    # doesn't clobber a global `--trace` given before the subcommand. resolve()/
    # out() merge the two positions.
    p.add_argument("--trace", dest="_gtrace", help=argparse.SUPPRESS)
    p.add_argument("--json", dest="_gjson", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    add("status").set_defaults(fn=cmd_status)
    u = add("use"); u.add_argument("trace_id"); u.set_defaults(fn=cmd_use)
    add("traces").set_defaults(fn=cmd_traces)
    add("precis").set_defaults(fn=cmd_precis)
    sk = add("skeleton"); sk.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD); sk.set_defaults(fn=cmd_skeleton)
    fr = add("frame"); fr.add_argument("n", type=int); fr.set_defaults(fn=cmd_frame)
    st = add("state"); st.add_argument("n", type=int); st.set_defaults(fn=cmd_state)
    lc = add("loc"); lc.add_argument("n", type=int); lc.set_defaults(fn=cmd_loc)
    sp = add("step"); sp.add_argument("n", type=int); sp.add_argument("--back", action="store_true"); sp.add_argument("--instant", type=int, default=None); sp.set_defaults(fn=cmd_step)
    vl = add("value"); vl.add_argument("n", type=int); vl.add_argument("--path", required=True, type=lambda s: json.loads(s) if s.strip().startswith("[") else s.split(".")); vl.add_argument("--instant", type=int, default=None); vl.add_argument("--depth", type=int, default=4); vl.set_defaults(fn=cmd_value)
    add("sources").set_defaults(fn=cmd_sources)
    so = add("source"); so.add_argument("--file", required=True); so.add_argument("--context", type=int, default=8); so.set_defaults(fn=cmd_source)
    ex = add("explain"); ex.add_argument("n", type=int); ex.add_argument("--question", default=None); ex.set_defaults(fn=cmd_explain)
    ho = add("handoff"); ho.add_argument("n", type=int); ho.add_argument("--question", default=None); ho.set_defaults(fn=cmd_handoff)
    rec = add("record")
    rec.add_argument("--crate"); rec.add_argument("--test")
    rec.add_argument("--lang", choices=["rust", "python"], default="rust")
    rec.add_argument("--script", help="python entrypoint (with --lang python)")
    rec.add_argument("--src", default="cairn", help="python user-source scope dir")
    rec.set_defaults(fn=cmd_record)
    add("derive").set_defaults(fn=cmd_derive)
    add("bench").set_defaults(fn=cmd_bench)
    df = add("diff"); df.add_argument("other", help="trace id to diff against the base (--trace or active)"); df.set_defaults(fn=cmd_diff)
    cof = add("capture-on-fail"); cof.add_argument("--cap", type=int, default=3, help="max failing tests to record"); cof.add_argument("--user-mark", default=None, help="source-path substring marking user code (default from project config, else /src/)"); cof.add_argument("--files", default=None, help="comma-separated file filter"); cof.add_argument("command", nargs=argparse.REMAINDER, help="-- <test command>"); cof.set_defaults(fn=cmd_capture_on_fail)
    cfx = add("capture-fix"); cfx.add_argument("red", help="the @red trace id to pair against"); cfx.add_argument("--bin", default=None, help="explicit rebuilt binary (else auto-located)"); cfx.set_defaults(fn=cmd_capture_fix)
    op = add("open"); op.add_argument("n", type=int, nargs="?"); op.set_defaults(fn=cmd_open)
    add("watch").set_defaults(fn=cmd_watch)
    pr = add("predict"); prs = pr.add_subparsers(dest="sub", required=True)
    pl = prs.add_parser("log", parents=[common]); pl.add_argument("--frame", required=True); pl.add_argument("--guessed", required=True); pl.add_argument("--truth", required=True); pl.add_argument("--correct", required=True); pl.set_defaults(fn=cmd_predict_log)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
        return EXIT_OK
    except CairnError as e:
        print(f"cairn: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
