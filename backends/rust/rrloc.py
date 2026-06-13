"""
Cairn tier-3 orchestrator.

Given an artifact frame, replays the ONE recording to that function's
activation and returns line-by-line DWARF state plus a reverse-step re-read.
Each call drives a fresh `rr replay` server + gdb client (tier 3 is the
on-demand deep dive, not the cheap overview).
"""

import json
import os
import re
import socket
import subprocess
import tempfile
import time

import layout

ROOT = layout.ROOT
RUST_DIR = os.path.join(ROOT, "backends", "rust")
# The compiled test binary (gdb symbol source). Supplied by env or the project
# config; when traces carry distinct binaries this moves under the trace.
BIN = os.environ.get("CAIRN_BIN") or layout.project_config().get("binary", "bin/test")
if not os.path.isabs(BIN):
    BIN = os.path.join(ROOT, BIN)


from errors import LocUnavailable  # shared core exception


_RUST_ENV = None


def _rust_printer_env():
    """Resolve the Rust toolchain's gdb pretty-printer dir once, so the loc
    agent renders Vec/String/etc. as contents. Cached; falls back silently."""
    global _RUST_ENV
    if _RUST_ENV is None:
        _RUST_ENV = {}
        if os.environ.get("CAIRN_RUST_ETC"):
            _RUST_ENV = {"CAIRN_RUST_ETC": os.environ["CAIRN_RUST_ETC"],
                         "CAIRN_RUST_SYSROOT": os.environ.get("CAIRN_RUST_SYSROOT", "")}
        else:
            try:
                sysroot = subprocess.check_output(
                    ["rustc", "--print", "sysroot"], text=True).strip()
                _RUST_ENV = {"CAIRN_RUST_SYSROOT": sysroot,
                             "CAIRN_RUST_ETC": os.path.join(sysroot, "lib", "rustlib", "etc")}
            except Exception:
                _RUST_ENV = {}
    return _RUST_ENV


def _trace_version(tid):
    """Identity of the recording AND the value-rendering code, so cache entries
    invalidate when the scenario is re-recorded OR the loc agent's formatting
    changes (else a renderer fix is masked by stale entries)."""
    parts = []
    for p in (layout.frames_path(tid), os.path.join(RUST_DIR, "loc_server.py")):
        try:
            parts.append(str(int(os.path.getmtime(p))))
        except OSError:
            parts.append("0")
    return "-".join(parts)


def _cache_path(tid, frame_id, instant):
    key = f"{frame_id}_{instant}_{_trace_version(tid)}.json"
    return os.path.join(layout.loc_cache_dir(tid), key)


def cached_loc(tid, frame_id, instant):
    p = _cache_path(tid, frame_id, instant)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _store_loc(tid, frame_id, instant, res):
    os.makedirs(layout.loc_cache_dir(tid), exist_ok=True)
    try:
        with open(_cache_path(tid, frame_id, instant), "w") as fh:
            json.dump(res, fh)
    except OSError:
        pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _body_start_line(file, decl_line):
    """First statement line of the function body, via source brace-matching.
    Breaking here (not the signature/multi-line params, and not the async
    poll-dispatch entry) lets `next` walk the body for sync AND async fns."""
    try:
        import otherarm
        import sources
        ap = sources.resolve(file)
        if not ap:
            return None
        src = open(ap, encoding="utf-8", errors="replace").read().splitlines()
        rng = otherarm._function_body(src, decl_line)
        if not rng:
            return None
        body_open = rng[0]            # 0-based index of the line holding `{`
        # advance past blank/`{`-only lines to the first real statement
        for k in range(body_open + 1, min(body_open + 8, rng[1] + 1)):
            if src[k].strip() not in ("", "{"):
                return k + 1          # 1-based
        return body_open + 2
    except Exception:
        return None


def _is_async_fn(file, decl_line):
    """True if the function at decl_line is `async fn` — read from source, the
    reliable signal. (The recorded symbol can be the sync constructor shim of
    an async fn, so the name alone misleads.)"""
    try:
        import sources
        ap = sources.resolve(file)
        if not ap:
            return False
        src = open(ap, encoding="utf-8", errors="replace").read().splitlines()
        # find the nearest `fn <name>` at or above the recorded decl line
        for k in range(min(decl_line, len(src)) - 1, max(-1, decl_line - 25), -1):
            if re.search(r"\bfn\s+[A-Za-z_]", src[k]):
                return bool(re.search(r"\basync\s+fn\b", src[k]))
        return False
    except Exception:
        return False


def _frame_activation(tid, frame):
    """Map an artifact frame -> (file, break_line, hit_index, decl_line) by
    re-reading the recorded stream. Artifact frames are the sorted rows in
    order, so frame['step'] indexes the sorted stream directly."""
    fp = layout.frames_path(tid)
    if not os.path.exists(fp):
        raise LocUnavailable("no rr frame stream; run `cairn record` first")
    rows = sorted((json.loads(l) for l in open(fp)),
                  key=lambda r: r["step"])
    idx = frame["step"]
    if idx >= len(rows):
        raise LocUnavailable("frame step out of range")
    row = rows[idx]
    decl = row["line"]
    # Sync fns: the recorded entry line walks fine. Async fns compile to a poll
    # state machine whose entry skips the body when stepped, so for those we
    # break at the first body statement instead. Detect async from source (the
    # symbol can be the sync shim of an async fn).
    is_async = _is_async_fn(row["file"], decl)
    brk = (_body_start_line(row["file"], decl) or decl) if is_async else decl
    return row["file"], brk, row.get("hit_index", 1), decl


import signal
import threading


class LocSession:
    """A warm gdb-under-rr-replay session reused across requests. Starting it
    pays the rr+gdb+DWARF cost ONCE; each request just restarts the sub-second
    replay over the open control socket. Serialized by a lock (one gdb)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._server = None     # rr replay -s
        self._gdb = None        # gdb client running loc_server.py
        self._conn = None       # control socket to the agent
        self._listener = None
        self._trace = None      # the trace this session is WARMED for

    def _alive(self):
        return (self._conn is not None and self._gdb and self._gdb.poll() is None
                and self._server and self._server.poll() is None)

    def _start(self, tid):
        rr = layout.rr_trace(tid)
        if not rr:
            raise LocUnavailable(f"no recording for trace `{tid}` "
                                 f"(run `cairn record`?)")
        port = _free_port()
        env = {**os.environ, "_RR_TRACE_DIR": layout.recording_dir(tid),
               **_rust_printer_env()}
        self._trace = tid
        self._server = subprocess.Popen(
            ["rr", "replay", "-s", str(port), "-k", rr],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        if not _wait_port(port):
            self._shutdown()
            raise LocUnavailable("rr replay server did not start")
        # Listener the gdb agent connects back to.
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        ctrl = self._listener.getsockname()[1]
        genv = {**env, "CAIRN_CTRL_PORT": str(ctrl)}
        self._gdb = subprocess.Popen(
            ["gdb", "-q", "-batch", "-ex", "set sysroot /",
             "-ex", f"target extended-remote 127.0.0.1:{port}",
             "-x", os.path.join(RUST_DIR, "loc_server.py"), BIN],
            cwd=ROOT, env=genv, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True)
        self._listener.settimeout(60)
        try:
            self._conn, _ = self._listener.accept()
        except socket.timeout:
            self._shutdown()
            raise LocUnavailable("loc agent did not connect")
        self._conn.settimeout(180)
        self._readline()        # consume the agent's {"ready":true}

    def _readline(self):
        buf = b""
        while b"\n" not in buf:
            chunk = self._conn.recv(65536)
            if not chunk:
                raise LocUnavailable("loc agent closed the connection")
            buf += chunk
        return buf.split(b"\n", 1)[0]

    def request(self, payload, tid):
        with self._lock:
            # The warm session is pinned to ONE recording. If the request is for
            # a different trace, tear down and re-warm — the session NEVER serves
            # state from a recording other than the one asked for (grounding).
            if not self._alive() or self._trace != tid:
                self._shutdown()
                self._start(tid)
            if self._trace != tid:                       # belt-and-braces guard
                raise LocUnavailable(
                    f"session refuses: warmed for `{self._trace}`, not `{tid}` "
                    f"— refusing rather than serving wrong-recording state")
            try:
                self._conn.sendall((json.dumps(payload) + "\n").encode())
                return json.loads(self._readline())
            except (OSError, LocUnavailable):
                self._shutdown()
                self._start(tid)
                self._conn.sendall((json.dumps(payload) + "\n").encode())
                return json.loads(self._readline())

    def warm(self, tid):
        """Explicit re-warm (cairn use) — tears down any session for another
        trace and starts one pinned to `tid`."""
        with self._lock:
            if not self._alive() or self._trace != tid:
                self._shutdown()
                self._start(tid)
            return self._trace

    def _shutdown(self):
        for proc in (self._gdb, self._server):
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        for s in (self._conn, self._listener):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self._server = self._gdb = self._conn = self._listener = None
        self._trace = None

    def close(self):
        with self._lock:
            self._shutdown()


_SESSION = LocSession()


def loc_for_frame(art, frame, instant=None):
    tid = art["scenario"]["id"]
    # The recording is immutable, so a frame's LOC is deterministic — serve
    # from cache when we have it (disk read instead of a replay round-trip).
    hit_cache = cached_loc(tid, frame["id"], instant)
    if hit_cache is not None:
        hit_cache["cached"] = True
        return hit_cache

    file, line, hit, decl = _frame_activation(tid, frame)
    res = _SESSION.request({"file": file, "line": line, "hit": hit,
                            "instant": instant}, tid)
    if "error" in res:
        raise LocUnavailable(f"loc agent: {res['error']}")

    res["frame_id"] = frame["id"]
    res["lane"] = frame["lane"]
    res["line_count"] = len(res.get("lines", []))
    _attach_source(res, file)
    res["cached"] = False
    _store_loc(tid, frame["id"], instant, res)
    return res


def resolve_value(art, frame, path, instant=None, depth=5):
    """Lazy-expand one value node deeper: re-reach the frame and decode the
    subtree named by `path`. Cached by (frame, instant, path, depth)."""
    if not path:
        raise LocUnavailable("empty path")
    tid = art["scenario"]["id"]
    key = f"{frame['id']}|{instant}|{depth}|{'/'.join(map(str, path))}"
    cached = cached_loc(tid, "val_" + _safe_key(key), None)
    if cached is not None:
        cached["cached"] = True
        return cached
    file, line, hit, decl = _frame_activation(tid, frame)
    res = _SESSION.request({"cmd": "resolve", "file": file, "line": line,
                            "hit": hit, "instant": instant, "path": path,
                            "depth": depth}, tid)
    if "error" in res:
        raise LocUnavailable(f"resolve: {res['error']}")
    res["frame_id"] = frame["id"]
    res["cached"] = False
    _store_loc(tid, "val_" + _safe_key(key), None, res)
    return res


def warm_trace(tid):
    """Re-warm the session for `tid` (cairn use). Returns the warmed trace id."""
    if not layout.rr_trace(tid):
        raise LocUnavailable(f"no recording for trace `{tid}`")
    return _SESSION.warm(tid)


def _safe_key(s):
    import hashlib
    return hashlib.sha1(s.encode()).hexdigest()[:24]


def _attach_source(res, file):
    """Attach the real source TEXT of each executed line so the tier-3 view is
    self-contained — the UI shouldn't need access to the crate sources. Reading
    the actual file is grounded (it's the code that ran); if unreadable we leave
    text absent rather than reconstruct it."""
    import sources
    ap = sources.resolve(file)
    if not ap:
        return
    try:
        src = open(ap, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return
    def text_at(n):
        return src[n - 1].rstrip() if 1 <= n <= len(src) else None
    for ln in res.get("lines", []):
        ln["text"] = text_at(ln["line"])
    rs = res.get("reverse_step")
    if isinstance(rs, dict) and rs.get("reread_line"):
        rs["reread_text"] = text_at(rs["reread_line"])
    res["source_path"] = file


def salient_frames(art, threshold=0.6):
    """One representative frame per lane above the threshold (matches the
    skeleton tier) — the frames a user is likely to open."""
    by_lane = {}
    for f in art["frames"]:
        if f["salience"] < threshold:
            continue
        cur = by_lane.get(f["lane"])
        if cur is None or f["salience"] > cur["salience"]:
            by_lane[f["lane"]] = f
    return sorted(by_lane.values(), key=lambda f: -f["salience"])


def precompute(art, threshold=0.6, log=lambda *a: None):
    """Warm the LOC cache for salient frames so the UI's first open is fast.
    Serial — the warm session is single gdb, so concurrency would just queue.
    Safe to call repeatedly (cache hits are skipped instantly)."""
    tid = art["scenario"]["id"]
    frames = [f for f in salient_frames(art, threshold)
              if cached_loc(tid, f["id"], None) is None]
    if not frames:
        return 0
    log(f"[cairn] precomputing LOC for {len(frames)} salient frames…")
    done = 0
    for f in frames:
        try:
            loc_for_frame(art, f, None)
            done += 1
        except Exception:
            pass
    log(f"[cairn] LOC precompute done: {done}/{len(frames)} cached")
    return done


import atexit
atexit.register(_SESSION.close)


if __name__ == "__main__":
    # CLI smoke test: dump LOC for a given frame id from the artifact.
    import sys
    art = json.load(open(layout.artifact_path(layout.resolve_active())))
    fid = sys.argv[1] if len(sys.argv) > 1 else None
    frame = next((f for f in art["frames"] if f["id"] == fid), None) if fid else \
        next(f for f in art["frames"] if "validate" in f["lane"] and f["kind"] == "branch")
    inst = int(sys.argv[2]) if len(sys.argv) > 2 else None
    res = loc_for_frame(art, frame, instant=inst)
    print(json.dumps(res, indent=2))
