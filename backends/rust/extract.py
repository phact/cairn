"""
Cairn frame-stream extractor (gdb-Python).

Runs INSIDE gdb. Identical code path whether driven by:
  - plain gdb (development):   gdb -q -batch -x extract.py BIN
  - rr replay  (real capture): rr replay -- -batch -x extract.py

It breaks on every function *defined in* a chosen set of user source files
(via `rbreak <file>:.`), runs the program, and at each stop records one
"enter" event grounded in the real frame: function name, file:line, decoded
argument values (DWARF), and stack depth. Output is JSONL to $CAIRN_OUT.

Nothing here infers control flow. Every emitted record is a real stop.
"""

import gdb
import json
import os
import re
import subprocess
import sys

# ---- configuration via env (so the same script serves both drivers) -------
OUT_PATH   = os.environ.get("CAIRN_OUT", "out/frames.jsonl")
MODE       = os.environ.get("CAIRN_MODE", "gdb")          # "gdb" | "rr"
TEST_NAME  = os.environ.get("CAIRN_TEST", "")              # for plain-gdb run args
# Source-file substrings to break on. These are the user-crate files the
# request flow actually traverses. Tune freely — this is the load-bearing
# "user_crate" filter applied at capture time.
FILE_GLOBS = [g for g in os.environ.get("CAIRN_FILES", "").split(",")
              if g.strip()]

# Per-function hit cap: hot loops collapse to N recorded samples + a count.
HIT_CAP    = int(os.environ.get("CAIRN_HIT_CAP", "40"))
# Absolute safety stop so a runaway never hangs the session.
MAX_EVENTS = int(os.environ.get("CAIRN_MAX_EVENTS", "200000"))
VAL_TRUNC  = 240

# ---------------------------------------------------------------------------
gdb.execute("set pagination off")
gdb.execute("set print pretty off")
gdb.execute("set print frame-arguments none")  # we read args ourselves
gdb.execute("set width 0")
gdb.execute("set confirm off")
# Don't stop on signals the tokio runtime uses internally.
for sig in ("SIGPIPE", "SIG32", "SIG33", "SIG34", "SIGUSR1", "SIGUSR2"):
    try:
        gdb.execute(f"handle {sig} nostop noprint pass")
    except gdb.error:
        pass


def load_rust_printers():
    """Load Rust's gdb pretty-printers so owned collections (Vec/String/Path/
    HashMap) decode to their CONTENTS, not RawVec internals. Same printers
    `rust-gdb` uses; gdb's built-ins only cover &str/slices/primitives."""
    try:
        etc = os.environ.get("CAIRN_RUST_ETC")
        sysroot = os.environ.get("CAIRN_RUST_SYSROOT")
        if not etc:
            sysroot = subprocess.check_output(
                ["rustc", "--print", "sysroot"], text=True).strip()
            etc = os.path.join(sysroot, "lib", "rustlib", "etc")
        if etc not in sys.path:
            sys.path.insert(0, etc)
        if sysroot:
            try:
                gdb.execute(f"add-auto-load-safe-path {sysroot}")
            except gdb.error:
                pass
        import gdb_load_rust_pretty_printers  # noqa: F401
    except Exception as e:
        try:
            print(f"[cairn] rust pretty-printers not loaded: {e}")
        except Exception:
            pass


load_rust_printers()


def truncate(s):
    s = s.replace("\n", " ")
    return s if len(s) <= VAL_TRUNC else s[:VAL_TRUNC] + "…"


_ADDR = re.compile(r"^\s*0x[0-9a-fA-F]+\s*$")
_AGG = None


def decode_value(val):
    """Decode a value into (display, ref). For pointer/reference types `ref` is
    {kind,address} (so the UI can disambiguate), and `display` shows the
    pointee's contents when it's an aggregate (e.g. a method's `&self`).
    Guarded — a dangling pointer keeps the address. Non-pointers: ref=None."""
    global _AGG
    if _AGG is None:
        _AGG = {gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION, gdb.TYPE_CODE_ENUM}
    s = str(val)
    ref = None
    try:
        t = val.type.strip_typedefs()
        code = t.code
        if code in (gdb.TYPE_CODE_REF, gdb.TYPE_CODE_PTR,
                    getattr(gdb, "TYPE_CODE_RVALUE_REF", -99)):
            kind = "ptr" if code == gdb.TYPE_CODE_PTR else "ref"
            try:
                address = hex(int(val))
            except Exception:
                address = s if _ADDR.match(s) else None
            ref = {"kind": kind, "address": address}
            if _ADDR.match(s) and t.target().strip_typedefs().code in _AGG:
                try:
                    s = str(val.dereference())
                except Exception:
                    pass
    except Exception:
        pass
    return s, ref


class FinishBP(gdb.FinishBreakpoint):
    """Fires when a recorded user frame returns, capturing its DWARF-decoded
    return value. Returns False so execution flows on (the value is the
    'deciding value' for decision functions; a real recorded fact)."""
    def __init__(self, frame, fn, enter_step, sink):
        super().__init__(frame, internal=True)
        self.silent = True
        self.fn = fn
        self.enter_step = enter_step
        self.sink = sink

    def stop(self):
        ref = None
        try:
            rv = self.return_value
            if rv is not None:
                disp, ref = decode_value(rv)
                val = truncate(disp)
            else:
                val = "<unit>"
        except Exception:
            val = "<unreadable>"
        rec = {"fn": self.fn, "enter_step": self.enter_step, "value": val}
        if ref:
            rec["ref"] = ref
        self.sink.append(rec)
        return False

    def out_of_scope(self):
        # frame unwound without a normal return (panic/async suspend); ignore.
        pass


def read_args(frame):
    """DWARF-decoded argument (name, value) pairs for this frame."""
    out = []
    try:
        block = frame.block()
    except RuntimeError:
        return out
    seen = set()
    while block is not None:
        for sym in block:
            if not sym.is_argument:
                continue
            if sym.name in seen:
                continue
            seen.add(sym.name)
            try:
                disp, ref = decode_value(sym.value(frame))
                rec = {"name": sym.name, "value": truncate(disp)}
                if ref:
                    rec["ref"] = ref
                out.append(rec)
            except Exception:
                out.append({"name": sym.name, "value": "<unreadable>"})
        if block.function is not None:
            break
        block = block.superblock
    return out


def stack_depth(frame, cap=256):
    d = 0
    f = frame
    while f is not None and d < cap:
        d += 1
        try:
            f = f.older()
        except Exception:
            break
    return d


# Fast depth: reconstruct call nesting from the STACK POINTER (one register read
# per stop) instead of walking 40+ frames (each an unwind = a remote round-trip).
# Stack grows down, so a deeper frame has a smaller sp; we keep a stack of live
# frame SPs and read off the depth. O(1) amortized, robust (no backtrace).
_SP_STACK = []


def cheap_depth(frame):
    try:
        sp = int(frame.read_register("sp"))
    except Exception:
        return stack_depth(frame)
    while _SP_STACK and _SP_STACK[-1] < sp:     # deeper frames have returned
        _SP_STACK.pop()
    if not _SP_STACK or _SP_STACK[-1] != sp:
        _SP_STACK.append(sp)
    return len(_SP_STACK)


# Only break on functions whose DEFINING file is under the user crate.
# gdb reports user-crate files as workspace-relative paths ("crates/<c>/src/..."),
# dependencies as absolute (/.cargo/registry/..., /.rustup/...). Requiring this
# substring is the load-bearing "user_crate" filter at capture time; the project
# config (or --user-mark) supplies the project's path, defaulting to "/src/".
USER_CRATE_MARK = os.environ.get("CAIRN_USER_MARK", "/src/")
# Alternative scoping: filter by demangled function-NAME prefix (e.g.
# "serde_json::") instead of source-file path. Robust when the crate-under-test
# is a *dependency of its own integration test* — gdb then reports its files as
# relative paths ("src/de.rs"), so a path mark can't distinguish it from std,
# but the name prefix always identifies it. Set CAIRN_NAME_MARK to use this.
NAME_MARK = os.environ.get("CAIRN_NAME_MARK", "")
# Structure-only fast extraction: skip per-frame arg value decode (the ranker
# never uses it; values are tier-3's lazy job). For big cross-crate traces.
STRUCT_ONLY = os.environ.get("CAIRN_STRUCT_ONLY") == "1"
# Arms-once: capture each user function's return arm on its FIRST hit, then
# disable that function's entry breakpoint so it never stops again. Turns the
# O(total calls) per-call cost into O(distinct functions) — the bounded arms
# pass the architecture specifies ("once per function suffices for flips"),
# needed for big traces (tera's 60k calls blow a per-call gdb pass).
ARMS_ONCE = os.environ.get("CAIRN_ARMS_ONCE") == "1"

_FILE_HDR = re.compile(r"^File (.+):$")
_FN_LINE  = re.compile(r"^(\d+):\s+(?:static\s+)?fn\s+(.+)$")


def enumerate_user_functions():
    """Parse `info functions` -> list of (file, line) defined in the user
    crate. Grounded in DWARF: a function is listed under the file header where
    it is actually defined.

    Two scoping modes: by source-file substring (USER_CRATE_MARK, default) or,
    when CAIRN_NAME_MARK is set, by demangled function-name prefix. The name
    mode passes the mark as a regex to `info functions` so gdb expands the
    dependency crate's compilation units (a bare `info functions` skips them)."""
    cmd = f"info functions {re.escape(NAME_MARK)}" if NAME_MARK else "info functions"
    text = gdb.execute(cmd, to_string=True)
    cur_file = None
    pairs = set()
    for raw in text.splitlines():
        m = _FILE_HDR.match(raw)
        if m:
            cur_file = m.group(1)
            continue
        if cur_file is None:
            continue
        fm = _FN_LINE.match(raw)
        if not fm:
            continue
        if NAME_MARK:
            if not fm.group(2).startswith(NAME_MARK):
                continue
        else:
            if USER_CRATE_MARK not in cur_file:
                continue
            if FILE_GLOBS and not any(g in cur_file for g in FILE_GLOBS):
                continue
        pairs.add((cur_file, int(fm.group(1))))
    return sorted(pairs)


def set_breakpoints():
    pairs = enumerate_user_functions()
    bps = []
    for f, ln in pairs:
        try:
            bp = gdb.Breakpoint(f"{f}:{ln}", type=gdb.BP_BREAKPOINT)
            bp.silent = True
            bps.append(bp)
        except gdb.error:
            pass
    files = sorted({f for f, _ in pairs})
    print(f"[cairn] set {len(bps)} breakpoints over {len(files)} user files:")
    for f in files:
        print(f"[cairn]   {f}")
    return bps


def start_program():
    if MODE == "rr":
        # Driven via `rr replay -s PORT`; gdb was already connected to the
        # replay server (target extended-remote) before this script ran.
        # The recording holds the args; just run forward from the start.
        gdb.execute("continue")
    else:
        args = f"--exact {TEST_NAME} --test-threads=1 --nocapture"
        gdb.execute(f"set args {args}")
        gdb.execute("run")


def inferior_alive():
    try:
        return any(t.is_valid() for t in gdb.selected_inferior().threads())
    except Exception:
        return False


def _addr_index(bps):
    """Map resolved entry address -> breakpoint, so we can disable a function's
    breakpoint after its first hit (arms-once). Built after the program starts,
    when pending breakpoints have resolved to addresses."""
    idx = {}
    for bp in bps:
        try:
            for loc in bp.locations:
                if getattr(loc, "address", None):
                    idx[loc.address] = bp
        except Exception:
            pass
    return idx


def main():
    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    bps = set_breakpoints()

    counts = {}
    returns = []
    step = 0
    fh = open(OUT_PATH, "w")
    addr_idx = {}
    try:
        start_program()
        if ARMS_ONCE:
            addr_idx = _addr_index(bps)
        while inferior_alive() and step < MAX_EVENTS:
            try:
                frame = gdb.newest_frame()
            except gdb.error:
                break
            try:
                fn = frame.function()
                name = fn.name if fn else (frame.name() or "<unknown>")
                sal = frame.find_sal()
                fname = sal.symtab.filename if (sal and sal.symtab) else "?"
                line = sal.line if sal else 0
            except Exception:
                name, fname, line = "<unknown>", "?", 0

            key = name
            c = counts.get(key, 0) + 1
            counts[key] = c

            if c <= HIT_CAP:
                # STRUCT_ONLY: the ranker needs structure (fn/file/line/depth) +
                # return arms, NOT per-frame arg values (role is name-derived;
                # frame values are tier-3's lazy job). Skipping read_args — the
                # heavy per-stop DWARF decode — is the big-trace speedup.
                rec = {
                    "step": step,
                    "fn": name,
                    "file": fname,
                    "line": line,
                    "depth": cheap_depth(frame) if STRUCT_ONLY else stack_depth(frame),
                    "args": [] if STRUCT_ONLY else read_args(frame),
                    "hit_index": c,
                }
                fh.write(json.dumps(rec) + "\n")
                # Arm a finish breakpoint to capture this frame's return value.
                try:
                    FinishBP(frame, name, step, returns)
                except (ValueError, gdb.error):
                    pass  # outermost frame / no return addr
                step += 1

            # arms-once: disable this function's entry breakpoint so subsequent
            # calls don't stop — the FinishBP just armed still captures THIS
            # activation's return. Bounds the run to one stop per function.
            if ARMS_ONCE:
                try:
                    bp = addr_idx.get(int(frame.pc()))
                    if bp is not None:
                        bp.enabled = False
                except Exception:
                    pass

            try:
                gdb.execute("continue")
            except gdb.error:
                break
    finally:
        fh.close()
    with open(OUT_PATH + ".returns", "w") as rf:
        json.dump(returns, rf)

    # Emit per-function totals as a trailer file for the ranker (frequency).
    with open(OUT_PATH + ".counts", "w") as cf:
        json.dump(counts, cf)
    print(f"[cairn] recorded {step} events; "
          f"{len(counts)} distinct user functions -> {OUT_PATH}")


main()
gdb.execute("quit")
