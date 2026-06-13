"""
Cairn capture/derive pipeline (core) — engine code called by the CLI and API.

Per-backend MECHANISMS (how to record, how to turn a recording into the
normalized frame stream) live behind the backend registry; this module owns
the SHARED tail every backend feeds: rank -> top20 -> artifact -> set active.

derive(tid) is the regenerability property: rm -rf derived/ && derive
reproduces a byte-identical artifact from recording/ alone.
"""

import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import layout
from errors import LocUnavailable

ROOT = layout.ROOT


class PipelineError(Exception):
    pass


# ---- shared helpers ----------------------------------------------------------
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


def _rust_env():
    try:
        sysroot = subprocess.check_output(["rustc", "--print", "sysroot"],
                                          text=True).strip()
        return {"CAIRN_RUST_SYSROOT": sysroot,
                "CAIRN_RUST_ETC": os.path.join(sysroot, "lib", "rustlib", "etc")}
    except Exception:
        return {}


def _bin():
    b = (os.environ.get("CAIRN_BIN")
         or layout.project_config().get("binary")
         or "bin/test")
    return b if os.path.isabs(b) else os.path.join(ROOT, b)


def _scope_env(crate):
    """Capture-scope env for the Rust extractor + ranker, derived from the
    project config (else generic). Lets the committed engine stay
    scenario-agnostic while a gitignored project.toml supplies the specifics."""
    proj = layout.project_config()
    out = {}
    mark = os.environ.get("CAIRN_USER_MARK") or proj.get("user_mark") \
        or (f"{crate}/src/" if crate else "/src/")
    out["CAIRN_USER_MARK"] = mark
    files = os.environ.get("CAIRN_FILES")
    if files is None:
        files = proj.get("files")
    if files:
        out["CAIRN_FILES"] = files
    strip = os.environ.get("CAIRN_STRIP_PREFIX") or proj.get("strip_prefix")
    if strip:
        out["CAIRN_STRIP_PREFIX"] = strip
    return out


# ---- the shared derive tail ----------------------------------------------------
def finish_derive(tid, backend, log=print):
    """rank -> top20 -> artifact, with the backend's declared env. Shared by
    every backend; the artifact's identity fields are DECLARED here, never
    guessed downstream."""
    os.makedirs(layout.derived_dir(tid), exist_ok=True)
    crate = tid.split("::", 1)[0]
    env = {**os.environ, **_scope_env(crate), **backend.artifact_env(tid)}
    log("[cairn] ranking …")
    top = subprocess.run([sys.executable, os.path.join(ROOT, "cairn", "rank.py")],
                         env=env, cwd=ROOT, capture_output=True, text=True)
    if top.returncode != 0:
        raise PipelineError(f"rank failed: {top.stderr[-400:]}")
    with open(layout.top20_path(tid), "w") as fh:
        fh.write(top.stdout)
    log("[cairn] building artifact …")
    art = subprocess.run([sys.executable, os.path.join(ROOT, "cairn", "artifact.py")],
                         env=env, cwd=ROOT, capture_output=True, text=True)
    if art.returncode != 0:
        raise PipelineError(f"artifact failed: {art.stderr[-400:]}")
    layout.write_active(tid)
    log(f"[cairn] active trace -> {tid}")
    return tid


def derive(tid, log=print):
    """(Re)build derived/ from the existing recording, whatever backend made it."""
    import backends
    try:
        backend = backends.detect(tid)
    except LocUnavailable as e:
        raise PipelineError(str(e))
    backend.extract(tid, log=log)
    return finish_derive(tid, backend, log=log)


# ---- rust mechanism ------------------------------------------------------------
def record_rust(crate, test, log=print):
    tid = f"{crate}::{test}"
    rec = layout.recording_dir(tid)
    shutil.rmtree(rec, ignore_errors=True)
    os.makedirs(rec, exist_ok=True)
    env = {**os.environ, "_RR_TRACE_DIR": rec}
    log(f"[cairn] recording {tid} (rr) …")
    try:
        subprocess.run(["rr", "record", _bin(), "--", "--exact", test,
                        "--test-threads=1", "--nocapture"],
                       check=True, env=env, cwd=ROOT)
    except subprocess.CalledProcessError as e:
        raise PipelineError(f"rr record failed: {e}")
    return derive(tid, log=log)


def extract_rust(tid, log=print):
    """Replay-extract the frame stream: rr's replay SERVER + our gdb client
    (rr injects its target connection after gdb -x, so order matters)."""
    rrtrace = layout.rr_trace(tid)
    if not rrtrace:
        raise PipelineError(f"no rr recording for `{tid}`")
    os.makedirs(layout.derived_dir(tid), exist_ok=True)
    crate, _, test = tid.partition("::")
    env = {**os.environ, **_rust_env(), **_scope_env(crate),
           "CAIRN_OUT": layout.frames_path(tid),
           "CAIRN_MODE": "rr", "CAIRN_TEST": test,
           "_RR_TRACE_DIR": layout.recording_dir(tid)}
    port = _free_port()
    server = subprocess.Popen(["rr", "replay", "-s", str(port), "-k", rrtrace],
                              env=env, cwd=ROOT, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        if not _wait_port(port):
            raise PipelineError("rr replay server did not start")
        log(f"[cairn] extracting frames from {os.path.basename(rrtrace)} …")
        proc = subprocess.run(
            ["gdb", "-q", "-batch", "-ex", "set sysroot /",
             "-ex", f"target extended-remote 127.0.0.1:{port}",
             "-x", os.path.join(ROOT, "backends", "rust", "extract.py"), _bin()],
            env=env, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True)
        if not os.path.exists(layout.frames_path(tid)):
            raise PipelineError("extraction produced no frame stream; "
                                f"gdb stderr: {proc.stderr.decode()[-400:]}")
    finally:
        try:
            os.killpg(os.getpgid(server.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# ---- python mechanism -----------------------------------------------------------
def record_python(script, src, log=print, name=None):
    script = os.path.abspath(script)
    src = os.path.abspath(src)
    stem = name or os.path.splitext(os.path.basename(script))[0]
    tid = f"python::{stem}"
    rec = layout.recording_dir(tid)
    shutil.rmtree(layout.trace_dir(tid), ignore_errors=True)
    os.makedirs(rec, exist_ok=True)
    log(f"[cairn] recording {tid} (py-monitoring) …")
    harness = os.path.join(ROOT, "backends", "python", "capture.py")
    proc = subprocess.run(
        [sys.executable, harness, "--script", script, "--src", src,
         "--frames-out", os.path.join(rec, "frames_py.jsonl"),
         "--eager-out", os.path.join(rec, "loc_eager.jsonl")],
        cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PipelineError(f"python capture failed: {proc.stderr[-600:]}")
    log(proc.stdout.strip())
    return derive(tid, log=log)


# back-compat name used by the CLI's rust path
record = record_rust
