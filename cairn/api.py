"""
Cairn tiered query API (Phase 2).

Every endpoint is a projection of ONE artifact (out/artifact.json), itself a
projection of ONE rr recording. Tiers cannot disagree because they are
re-derived from the same source of truth.

  POST /record                              -> { scenario_id }   (runs pipeline)
  GET  /scenario/:id                        -> full artifact
  GET  /scenario/:id/precis                 -> tier 1 (precis + boundaries)
  GET  /scenario/:id/skeleton?threshold=.6  -> tier 2 (salient frames + edges)
  GET  /scenario/:id/frame/:fid/loc         -> tier 3 (line-by-line + DWARF state)
  GET  /scenario/:id/frame/:fid/loc/step/:n -> state at a replay instant (reverse-step)
  POST /scenario/:id/predictions            -> log a prediction outcome

Stdlib only (http.server) so it runs with no install.
"""

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layout  # noqa: E402


def load_artifact(tid=None):
    """The artifact for a trace (the requested one, else the active one). Read
    from disk per request so it's never stale."""
    tid = tid or layout.resolve_active()
    if tid is None:
        raise FileNotFoundError("no active trace")
    with open(layout.artifact_path(tid)) as fh:
        return json.load(fh)


def tier_precis(art):
    return {"precis": art["precis"], "boundaries": art["boundaries"]}


def tier_skeleton(art, threshold):
    above = [f for f in art["frames"] if f["salience"] >= threshold]
    # Collapse to one representative per logical lane (most salient, then
    # earliest) so the swimlane shows the path once, not every async re-poll.
    by_lane = {}
    for f in above:
        cur = by_lane.get(f["lane"])
        if cur is None or (f["salience"], -f["step"]) > (cur["salience"], -cur["step"]):
            by_lane[f["lane"]] = f
    skeleton = sorted(by_lane.values(), key=lambda f: (-f["salience"], f["step"]))
    keep_lanes = set(by_lane)
    # Project instance edges onto lanes so the swimlane stays connected even
    # after collapsing async re-polls to one representative per lane.
    lane_of = {f["id"]: f["lane"] for f in art["frames"]}
    seen = set()
    edges = []
    for e in art["causal_edges"]:
        fl, tl = lane_of.get(e["from"]), lane_of.get(e["to"])
        if fl in keep_lanes and tl in keep_lanes and fl != tl:
            key = (fl, tl, e["relation"])
            if key not in seen:
                seen.add(key)
                edges.append({"from": fl, "to": tl, "relation": e["relation"]})
    return {"threshold": threshold, "frames": skeleton, "causal_edges": edges,
            "count": len(skeleton)}


def find_frame(art, fid):
    return next((f for f in art["frames"] if f["id"] == fid), None)


def artifact_files(art):
    """Source files referenced by the recording — the ONLY files the source
    endpoint will serve (prevents arbitrary reads / path traversal)."""
    return {f["file"] for f in art["frames"]
            if f.get("file") not in ("?", "", None)}


def tier_sources(art):
    """List the source files the UI can fetch, with how much of each ran."""
    counts = {}
    lane_by_file = {}
    for f in art["frames"]:
        fp = f.get("file")
        if fp in ("?", "", None):
            continue
        counts[fp] = counts.get(fp, 0) + 1
        lane_by_file.setdefault(fp, set()).add(f["lane"])
    return {"files": [
        {"path": fp, "frame_count": counts[fp], "lanes": sorted(lane_by_file[fp])}
        for fp in sorted(counts)]}


def serve_source(art, path):
    """Return a whole file's source as numbered lines, plus which lines the
    recording executed (so the UI can highlight covered code). Only files in
    the artifact are served."""
    files = artifact_files(art)
    # accept either the full recorded path or a lane-relative suffix
    if path not in files:
        match = [f for f in files if f.endswith(path) or f.endswith("/" + path)]
        if len(match) != 1:
            return None
        path = match[0]
    import sources
    ap = sources.resolve(path)
    if not ap:
        return None
    try:
        text = open(ap, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    executed = sorted({f["line"] for f in art["frames"]
                       if f.get("file") == path and f.get("line")})
    lines = [{"n": i, "text": t}
             for i, t in enumerate(text.splitlines(), 1)]
    return {"path": path, "line_count": len(lines),
            "executed_lines": executed, "lines": lines}


def build_explain_context(art, frame, question=""):
    """Grounded context for an LLM to narrate this frame faithfully: the code,
    the recorded values (with our deterministic decodes), the branch outcome,
    the scenario précis, plus a grounding-first prompt. No LLM is called — the
    backend serves real facts; the model summarizes them, never inventing."""
    fid = frame["id"]
    fn = frame["lane"].split("::")[-1]
    caps = art.get("capabilities", {})

    code, state, reverse = [], frame.get("values", []), None
    if caps.get("loc_state", True):
        try:
            import backends
            res = backends.for_artifact(art).loc(art, frame, instant=None)
            # the executed source lines of this function
            seen = set()
            for ln in res.get("lines", []):
                key = ln["line"]
                if key in seen:
                    continue
                seen.add(key)
                code.append({"line": ln["line"], "text": ln.get("text", "")})
            # richest recorded locals (the line with the most state)
            best = max(res.get("lines", []), key=lambda l: len(l.get("locals", [])),
                       default=None)
            if best:
                state = best["locals"]
            reverse = res.get("reverse_step")
        except Exception:
            pass

    ctx = {
        "scenario": art["scenario"]["id"],
        "language": art["scenario"].get("language"),
        "frame": {"id": fid, "function": fn, "file": frame["file"],
                  "line": frame["line"], "kind": frame["kind"],
                  "role": frame.get("role"), "salience": frame.get("salience")},
        "precis": art.get("precis"),
        "code": code or None,
        "state": state,
        "branch": frame.get("branch"),
        "reverse_step": reverse,
        "question": question or None,
    }
    ctx["prompt"] = _explain_prompt(ctx)
    ctx["grounding"] = ("Every value, branch arm, and line above is real recorded "
                        "state from an rr time-travel recording. Explain only from "
                        "these facts; never invent values, control flow, or behavior "
                        "that isn't shown. Say so if something isn't recorded.")
    return ctx


def _val_line(v):
    s = f"  {v['name']} = {v.get('value', '')}"
    return s


def _explain_prompt(ctx):
    f = ctx["frame"]
    L = [f"You are narrating a REAL recorded execution (rr time-travel debugging) "
         f"so a developer can understand code they didn't write. Explain what is "
         f"happening at this point using ONLY the recorded facts below — never "
         f"infer values or control flow that isn't shown.",
         "",
         f"Scenario: {ctx['scenario']}",
         f"Précis: {ctx.get('precis')}",
         "",
         f"Function `{f['function']}` at {f['file']}:{f['line']} (kind: {f['kind']})."]
    if ctx.get("code"):
        L.append("Executed source lines:")
        for c in ctx["code"]:
            L.append(f"  {c['line']}: {c['text']}")
    if ctx.get("state"):
        L.append("Recorded state here (values are real):")
        for v in ctx["state"][:24]:
            L.append(_val_line(v))
    b = ctx.get("branch")
    if b:
        L.append(f"Branch: took arm `{b.get('taken_arm')}`; deciding value "
                 f"`{b.get('deciding_value')}`.")
        nt = b.get("not_taken")
        if nt:
            L.append(f"Path not taken: arm `{nt.get('arm')}` — {nt.get('summary')}.")
    if ctx.get("question"):
        L.append("")
        L.append(f"The developer asks: {ctx['question']}")
        L.append("Answer their question, grounded in the facts above.")
    else:
        L.append("")
        L.append("Give a 2-4 sentence explanation of what this line does and why, "
                 "grounded in the recorded values.")
    return "\n".join(L)


def config_defaults():
    """Configurable inputs (with current defaults) so the UI can prefill a
    'record' form. The project config (gitignored `.cairn/project.toml`)
    supplies the scenario; env overrides it; the engine names none itself."""
    proj = layout.project_config()
    return {
        "crate": os.environ.get("CAIRN_CRATE") or proj.get("crate", ""),
        "test_name": os.environ.get("CAIRN_TEST") or proj.get("default_test", ""),
        "binary": os.environ.get("CAIRN_BIN") or proj.get("binary", ""),
        "src_root": os.environ.get("CAIRN_SRC_ROOT", ""),
        "mode": "rr",
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        q = parse_qs(u.query)
        try:
            if parts == ["config"]:
                return self._send(200, config_defaults())
            if parts == ["status"]:
                # Scenario discovery for the UI: the active trace + everything
                # available, so the frontend never hardcodes/guesses an id.
                return self._send(200, {"active_trace": layout.resolve_active(),
                                        "traces": layout.list_traces()})
            if parts == ["diff"]:
                # A/B divergence between two recordings: deciding frames
                # (outcome flips), consequences, presence diff, path split,
                # and the ranker check. ?a=<tid>&b=<tid>
                a = q.get("a", [None])[0] or layout.resolve_active()
                b = q.get("b", [None])[0]
                if not (a and b):
                    return self._send(400, {"error": "diff needs ?a=<trace>&b=<trace>",
                                            "have": layout.list_traces()})
                try:
                    import tracediff
                    return self._send(200, tracediff.diff(a, b))
                except Exception as e:
                    return self._send(404, {"error": str(e),
                                            "have": layout.list_traces()})
            if parts[:1] == ["scenario"] and len(parts) >= 2:
                try:
                    art = load_artifact(parts[1])     # load THIS trace by id
                except (FileNotFoundError, OSError):
                    return self._send(404, {"error": "unknown trace",
                                            "id": parts[1],
                                            "active_trace": layout.resolve_active(),
                                            "have": layout.list_traces()})
                # /scenario/:id
                if len(parts) == 2:
                    return self._send(200, art)
                sub = parts[2]
                if sub == "precis":
                    return self._send(200, tier_precis(art))
                if sub == "skeleton":
                    th = float(q.get("threshold", ["0.6"])[0])
                    return self._send(200, tier_skeleton(art, th))
                if sub == "sources":
                    return self._send(200, tier_sources(art))
                if sub == "source":
                    path = q.get("path", [""])[0]
                    if not path:
                        return self._send(400, {"error": "source needs ?path=<file>"})
                    src = serve_source(art, path)
                    if src is None:
                        return self._send(404, {"error": "file not in this recording",
                                                "path": path})
                    return self._send(200, src)
                if sub == "frame" and len(parts) >= 5 and parts[4] == "loc":
                    fid = parts[3]
                    frame = find_frame(art, fid)
                    if not frame:
                        return self._send(404, {"error": "unknown frame", "fid": fid})
                    # /frame/:fid/loc/step/:n  (reverse-step instant)
                    if len(parts) >= 7 and parts[5] == "step":
                        return self._loc(art, frame, instant=int(parts[6]))
                    return self._loc(art, frame, instant=None)
                # /frame/:fid/value?path=<json>&instant=N&depth=D — lazy expand
                if sub == "frame" and len(parts) >= 5 and parts[4] == "value":
                    fid = parts[3]
                    frame = find_frame(art, fid)
                    if not frame:
                        return self._send(404, {"error": "unknown frame", "fid": fid})
                    return self._resolve_value(art, frame, q)
                # /frame/:fid/explain?q=<question> — grounded narration context
                if sub == "frame" and len(parts) >= 5 and parts[4] == "explain":
                    fid = parts[3]
                    frame = find_frame(art, fid)
                    if not frame:
                        return self._send(404, {"error": "unknown frame", "fid": fid})
                    return self._explain(art, frame, q)
            return self._send(404, {"error": "not found", "path": u.path})
        except FileNotFoundError:
            return self._send(409, {"error": "no artifact yet; POST /record first"})
        except Exception as e:  # noqa
            return self._send(500, {"error": str(e)})

    def _loc(self, art, frame, instant):
        """Tier 3 — delegate to the rr-driven LOC/reverse-step extractor."""
        import backends
        from errors import LocUnavailable
        try:
            res = backends.for_artifact(art).loc(art, frame, instant=instant)
            return self._send(200, res)
        except LocUnavailable as e:
            return self._send(501, {"error": str(e), "frame": frame})

    def _explain(self, art, frame, q):
        """Assemble the GROUNDED context an LLM needs to faithfully explain this
        frame, plus a ready prompt — and call no LLM itself. The backend serves
        real recorded facts; narration is the downstream model's job (embedded,
        MCP tool, or the user's coding harness). Per the spec: the model
        summarizes something real and must never invent what wasn't recorded."""
        question = (q.get("q", [""])[0] or "").strip()
        context = build_explain_context(art, frame, question)
        return self._send(200, context)

    def _resolve_value(self, art, frame, q):
        """Lazy value expansion — resolve a clipped tree node deeper on click.
        `path` is the JSON accessor list the tree node carried."""
        import backends
        from errors import LocUnavailable
        raw = q.get("path", ["[]"])[0]
        try:
            path = json.loads(raw)
            assert isinstance(path, list)
        except Exception:
            return self._send(400, {"error": "path must be a JSON array"})
        instant = q.get("instant", [None])[0]
        instant = int(instant) if instant not in (None, "") else None
        depth = int(q.get("depth", ["5"])[0])
        try:
            res = backends.for_artifact(art).resolve_value(
                art, frame, path, instant=instant, depth=depth)
            return self._send(200, res)
        except LocUnavailable as e:
            return self._send(501, {"error": str(e)})

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad JSON body"})

        if parts == ["session", "use"]:
            # Switch the active trace and re-warm the LocSession so tier-3 is
            # pinned to the right recording (never serves wrong-recording state).
            tid = body.get("trace")
            if not tid:
                return self._send(400, {"error": "trace required"})
            layout.write_active(tid)
            try:
                import backends
                warmed = backends.detect(tid).warm(tid)
                return self._send(200, {"trace": warmed, "warmed": True})
            except Exception as e:
                return self._send(409, {"error": f"could not warm `{tid}`: {e}"})

        if parts == ["record"]:
            d = config_defaults()
            crate = body.get("crate", d["crate"])
            test = body.get("test_name", d["test_name"])
            # env overrides flow into the engine the same way the CLI's do
            for k, bk in (("CAIRN_SRC_ROOT", "src_root"), ("CAIRN_BIN", "binary")):
                if body.get(bk):
                    os.environ[k] = body[bk]
            try:
                import pipeline
                tid = pipeline.record(crate, test, log=lambda *a: None)
                return self._send(200, {"scenario_id": tid})
            except Exception as e:
                return self._send(500, {"error": f"pipeline failed: {e}"})

        if parts[:1] == ["scenario"] and parts[-1:] == ["predictions"]:
            rec = {"frame_id": body.get("frame_id"), "guessed": body.get("guessed"),
                   "truth": body.get("truth"), "correct": body.get("correct"),
                   "frame_kind": None}
            try:
                art = load_artifact()
                fr = find_frame(art, rec["frame_id"])
                rec["frame_kind"] = fr["kind"] if fr else None
            except FileNotFoundError:
                pass
            pred_log = layout.predictions_path()       # root-level, cross-trace
            os.makedirs(os.path.dirname(pred_log), exist_ok=True)
            with open(pred_log, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            return self._send(200, {"logged": rec})

        return self._send(404, {"error": "not found", "path": u.path})


def _warm_cache():
    """Background: warm the LOC cache for salient frames so the UI's first
    open of any swimlane frame is instant. Non-blocking; the server is usable
    immediately and fills in behind the scenes."""
    try:
        import backends
        art = load_artifact()
        backends.for_artifact(art).precompute(art, log=lambda *a: print(*a, flush=True))
    except Exception as e:  # no artifact yet, etc. — just skip
        print(f"[cairn] precompute skipped: {e}", flush=True)


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    port = int(os.environ.get("CAIRN_PORT", "8787"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Cairn API on http://127.0.0.1:{port}")
    if os.environ.get("CAIRN_NO_PRECOMPUTE") != "1":
        import threading
        threading.Thread(target=_warm_cache, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
