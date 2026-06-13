"""
Cairn Python capture backend (sys.monitoring, PEP 669).

Runs a Python entrypoint under monitoring and records:
  1. the normalized FrameStream (frames + .counts + .returns) the core
     rank/artifact consume unchanged, and
  2. EAGER per-line state for the first N activations of each user function —
     the hybrid-capability path: with no time-travel debugger, tier-3
     "line-by-line + reverse-step" is served by INDEXING this recorded data.

Grounding note on lossy values: a value clipped by the eager depth budget is
NOT retrievable after the process exits (unlike rr, where an unexpanded
pointer can be re-followed). Clipped nodes therefore carry
`deref.status: "unreadable"` — terminal, no expand affordance — never
"unexpanded", which would promise a retrievability this backend doesn't have.

Usage (driven by cairn/pipeline.py):
  python3 capture.py --script <entry.py> --src <user-source-dir>
                     --frames-out <frames.jsonl> --eager-out <loc_eager.jsonl>
"""

import argparse
import json
import os
import runpy
import sys

_CAIRN_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "cairn")
if _CAIRN_DIR not in sys.path:
    sys.path.insert(0, _CAIRN_DIR)
import decoders  # shared decode SPECS — core-owned, no drift

mon = sys.monitoring
TOOL = mon.PROFILER_ID

VAL_CEIL = 1 << 20                 # 1 MiB safety ceiling, marked when tripped
LOC_HIT_CAP = int(os.environ.get("CAIRN_PY_LOC_HITS", "3"))     # activations/fn
LOC_LINE_CAP = int(os.environ.get("CAIRN_PY_LOC_LINES", "150"))  # lines/activation
TREE_DEPTH = int(os.environ.get("CAIRN_PY_TREE_DEPTH", "3"))
TREE_NODES = int(os.environ.get("CAIRN_PY_TREE_NODES", "150"))   # per line
TREE_CHILDREN = 32


def _ceil(s):
    return s if len(s) <= VAL_CEIL else s[:VAL_CEIL] + "…[clipped]"


def _repr(obj):
    try:
        return _ceil(repr(obj))
    except Exception:
        return f"<unrepr {type(obj).__name__}>"


# ---- deterministic decoders: SPECS shared from core (cairn/decoders.py) -----
_jwt = decoders.jwt_decode
_timeish = decoders.timeish_name
_epoch_iso = decoders.epoch_iso


# ---- value tree (same node schema the Rust backend emits) -------------------
_PRIMITIVE = (type(None), bool, int, float, complex)


def tree(obj, depth=TREE_DEPTH, budget=None, seen=None, name=None):
    if budget is None:
        budget = {"n": 0}
    if seen is None:
        seen = set()
    budget["n"] += 1

    if isinstance(obj, _PRIMITIVE):
        node = {"summary": _repr(obj)}
        if isinstance(obj, int) and not isinstance(obj, bool) and _timeish(name):
            iso = _epoch_iso(obj)
            if iso:
                node["as_time"] = iso
        return node
    if isinstance(obj, (str, bytes)):
        node = {"summary": _repr(obj)}
        j = _jwt(obj if isinstance(obj, str) else "")
        if j:
            node["jwt"] = j
        return node

    oid = id(obj)
    if oid in seen:
        return {"summary": "↩ cycle", "deref": {"status": "expanded",
                                                "kind": "ref", "addr": hex(oid)}}
    if depth <= 0 or budget["n"] > TREE_NODES:
        # NOT retrievable post-run (eager capture) -> terminal, never "unexpanded"
        return {"summary": _ceil(_repr(obj)[:300]), "truncated": True,
                "deref": {"status": "unreadable", "kind": "ref", "addr": hex(oid),
                          "reason": "not recorded deeper (eager capture depth budget)"}}
    seen = seen | {oid}

    def kid(name_, v):
        c = tree(v, depth - 1, budget, seen, name=name_)
        c["name"] = name_
        return c

    if isinstance(obj, dict):
        node = {"summary": f"dict({len(obj)})", "children": []}
        for i, (k, v) in enumerate(obj.items()):
            if i >= TREE_CHILDREN or budget["n"] > TREE_NODES:
                node["truncated"] = True
                break
            node["children"].append(kid(_repr(k)[:48], v))
        return node
    if isinstance(obj, (list, tuple, set, frozenset)):
        node = {"summary": f"{type(obj).__name__}({len(obj)})", "children": []}
        for i, v in enumerate(obj):
            if i >= TREE_CHILDREN or budget["n"] > TREE_NODES:
                node["truncated"] = True
                break
            node["children"].append(kid(f"[{i}]", v))
        return node
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict) and d:
        node = {"summary": type(obj).__name__, "type": type(obj).__name__,
                "children": []}
        for i, (k, v) in enumerate(d.items()):
            if i >= TREE_CHILDREN or budget["n"] > TREE_NODES:
                node["truncated"] = True
                break
            node["children"].append(kid(str(k), v))
        return node
    return {"summary": _repr(obj)}


def _flatten_locals(frame_locals):
    out = []
    for k, v in list(frame_locals.items())[:40]:
        t = tree(v, name=k)
        rec = {"name": k, "value": _ceil(_repr(v)[:4096]),
               "kind": "local", "tree": t}
        if t.get("as_time"):
            rec["as_time"] = t["as_time"]
        out.append(rec)
    return out


def _find_frame(code):
    f = sys._getframe()
    while f is not None and f.f_code is not code:
        f = f.f_back
    return f


def _logical(qualname):
    return qualname.replace(".<locals>.", ".")


def _outcome(retval):
    if retval is None:
        return {"arm": "none", "display": "None"}
    if retval is True:
        return {"arm": "true", "display": "True"}
    if retval is False:
        return {"arm": "false", "display": "False"}
    if isinstance(retval, dict):
        return {"arm": "some", "display": f"dict({len(retval)} keys)"}
    if isinstance(retval, (list, tuple, set)):
        return {"arm": "some", "display": f"{type(retval).__name__}({len(retval)})"}
    if isinstance(retval, (int, float, str, bytes)):
        return {"arm": "some", "display": _repr(retval)[:48]}
    return {"arm": "some", "display": type(retval).__name__}


class Capture:
    def __init__(self, src_root):
        self.src = os.path.abspath(src_root) + os.sep
        self.frames, self.returns, self.counts = [], [], {}
        self.step, self.depth = 0, 0
        self._enter = []          # stack of enter-steps for active user frames
        self._recorders = {}      # id(frame) -> {"fn","hit","file","lines":[…]}
        self.eager = []           # finalized per-activation line recordings

    def _is_user(self, code):
        return code.co_filename.startswith(self.src)

    # -- enter ---------------------------------------------------------------
    def on_start(self, code, offset):
        if not self._is_user(code):
            return
        fn = code.co_qualname
        c = self.counts.get(fn, 0) + 1
        self.counts[fn] = c
        frame = _find_frame(code)
        args = []
        if frame is not None:
            for nm in code.co_varnames[:code.co_argcount]:
                if nm in frame.f_locals:
                    args.append({"name": nm,
                                 "value": _repr(frame.f_locals[nm])[:4096]})
        self.frames.append({"step": self.step, "fn": fn,
                            "fn_logical": _logical(fn),
                            "file": code.co_filename,
                            "line": code.co_firstlineno,
                            "depth": self.depth, "args": args, "hit_index": c})
        # eager per-line recorder for the first LOC_HIT_CAP activations
        if c <= LOC_HIT_CAP and frame is not None:
            self._recorders[id(frame)] = {"fn": fn, "hit": c,
                                          "file": code.co_filename, "lines": []}
        self._enter.append(self.step)
        self.step += 1
        self.depth += 1

    # -- per line -------------------------------------------------------------
    def on_line(self, code, line):
        if not self._is_user(code):
            return
        frame = _find_frame(code)
        if frame is None:
            return
        rec = self._recorders.get(id(frame))
        if rec is None or len(rec["lines"]) >= LOC_LINE_CAP:
            return
        rec["lines"].append({"i": len(rec["lines"]), "line": line,
                             "locals": _flatten_locals(frame.f_locals),
                             "fn": code.co_qualname})

    # -- exit -----------------------------------------------------------------
    def _finish(self, code, retval=None, unwound=False):
        if self.depth > 0:
            self.depth -= 1
        enter = self._enter.pop() if self._enter else None
        frame = _find_frame(code)
        rec = self._recorders.pop(id(frame), None) if frame is not None else None
        if rec is not None:
            self.eager.append(rec)
        if not unwound:
            self.returns.append({"fn": code.co_qualname,
                                 "fn_logical": _logical(code.co_qualname),
                                 "enter_step": enter, "value": _repr(retval),
                                 "outcome": _outcome(retval)})

    def on_return(self, code, offset, retval):
        if self._is_user(code):
            self._finish(code, retval)

    def on_unwind(self, code, offset, exc):
        if self._is_user(code):
            self._finish(code, unwound=True)

    # -- lifecycle ------------------------------------------------------------
    def install(self):
        mon.use_tool_id(TOOL, "cairn-py")
        ev = mon.events
        mon.set_events(TOOL, ev.PY_START | ev.PY_RETURN | ev.PY_UNWIND | ev.LINE)
        mon.register_callback(TOOL, ev.PY_START, self.on_start)
        mon.register_callback(TOOL, ev.PY_RETURN, self.on_return)
        mon.register_callback(TOOL, ev.PY_UNWIND, self.on_unwind)
        mon.register_callback(TOOL, ev.LINE, self.on_line)

    def uninstall(self):
        mon.set_events(TOOL, mon.events.NO_EVENTS)
        for e in (mon.events.PY_START, mon.events.PY_RETURN,
                  mon.events.PY_UNWIND, mon.events.LINE):
            mon.register_callback(TOOL, e, None)
        mon.free_tool_id(TOOL)

    def dump(self, frames_out, eager_out):
        os.makedirs(os.path.dirname(frames_out), exist_ok=True)
        with open(frames_out, "w") as fh:
            for r in self.frames:
                fh.write(json.dumps(r) + "\n")
        with open(frames_out + ".counts", "w") as fh:
            json.dump(self.counts, fh)
        with open(frames_out + ".returns", "w") as fh:
            json.dump(self.returns, fh)
        with open(eager_out, "w") as fh:
            for r in self.eager:
                fh.write(json.dumps(r) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--src", required=True, help="user-source scope (prefix)")
    ap.add_argument("--frames-out", required=True)
    ap.add_argument("--eager-out", required=True)
    args = ap.parse_args()

    cap = Capture(args.src)
    cap.install()
    try:
        runpy.run_path(args.script, run_name="__main__")
    finally:
        cap.uninstall()
    cap.dump(args.frames_out, args.eager_out)
    print(f"[cairn-py] {len(cap.frames)} frames, {len(cap.counts)} functions, "
          f"{len(cap.eager)} eager line-recordings")


if __name__ == "__main__":
    main()
