#!/usr/bin/env python3
"""
Corpus recorder — one test -> a derived frame stream for the training corpus.

  rr record <bin> --exact <test>   (fast)
  -> gdb structural extract scoped to <user_mark>   (STRUCT_ONLY: structure +
     return arms, no per-frame value decode)
  -> save frames.jsonl(+.counts/.returns) under .cairn/corpus/<repo>/<test>/
  -> PRUNE the rr recording (keep derived; corpus is the frame streams, not GBs
     of recordings)

Tolerant: any failure is logged and returns None; the campaign moves on.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, ".cairn", "corpus")
EXTRACT = os.path.join(ROOT, "backends", "rust", "extract.py")


def _rust_env():
    try:
        sr = subprocess.check_output(["rustc", "--print", "sysroot"], text=True).strip()
        return {"CAIRN_RUST_SYSROOT": sr,
                "CAIRN_RUST_ETC": os.path.join(sr, "lib", "rustlib", "etc")}
    except Exception:
        return {}


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _wait_port(port, t=15):
    end = time.time() + t
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def record_test(repo, binary, test, user_mark, src_root, rec_timeout=120,
                ext_timeout=150):
    """Record + extract one test. Returns {frames, fns, deciders_in_returns} or None."""
    binary = os.path.abspath(binary)
    out = os.path.join(CORPUS, repo, test.replace("::", "_").replace("/", "_"))
    os.makedirs(out, exist_ok=True)
    rec = os.path.join(out, "_rec")
    shutil.rmtree(rec, ignore_errors=True); os.makedirs(rec)
    env = {**os.environ, "_RR_TRACE_DIR": rec}
    try:
        subprocess.run(["rr", "record", binary, "--", "--exact", test,
                        "--test-threads=1", "--nocapture"], env=env, cwd=ROOT,
                       timeout=rec_timeout, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except Exception as e:
        shutil.rmtree(rec, ignore_errors=True); return None
    # find rr trace
    rrt = None
    for n in sorted(os.listdir(rec)):
        d = os.path.join(rec, n)
        if os.path.isdir(d) and not os.path.islink(d) and os.path.exists(os.path.join(d, "version")):
            rrt = d; break
    if not rrt:
        shutil.rmtree(rec, ignore_errors=True); return None
    # replay-extract (structure only)
    frames_path = os.path.join(out, "frames.jsonl")
    port = _free_port()
    srv = subprocess.Popen(["rr", "replay", "-s", str(port), "-k", rrt], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           start_new_session=True)
    try:
        if not _wait_port(port):
            return None
        # A "crate::" mark scopes by function-name prefix (robust when the
        # crate is a dependency of its own integration test); a path mark
        # ("box-agent/src/") scopes by source file.
        name_mark = user_mark if "::" in user_mark else ""
        eenv = {**os.environ, **_rust_env(), "CAIRN_MODE": "rr", "CAIRN_STRUCT_ONLY": "1",
                "CAIRN_OUT": frames_path, "CAIRN_SRC_ROOT": src_root, "CAIRN_FILES": "",
                "CAIRN_USER_MARK": "" if name_mark else user_mark,
                "CAIRN_NAME_MARK": name_mark}
        subprocess.run(["gdb", "-q", "-batch", "-ex", "set sysroot /",
                        "-ex", f"target extended-remote 127.0.0.1:{port}",
                        "-x", EXTRACT, binary], env=eenv, cwd=ROOT,
                       stdin=subprocess.DEVNULL, timeout=ext_timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    finally:
        try:
            os.killpg(os.getpgid(srv.pid), signal.SIGKILL)
        except Exception:
            pass
        shutil.rmtree(rec, ignore_errors=True)        # prune recording (disk)
    if not os.path.exists(frames_path):
        return None
    n = sum(1 for _ in open(frames_path))
    fns = len({json.loads(l)["fn"] for l in open(frames_path)})
    rets = []
    rp = frames_path + ".returns"
    if os.path.exists(rp):
        try: rets = json.load(open(rp))
        except Exception: pass
    return {"frames": n, "fns": fns, "returns": len(rets), "out": out}


def disk_free_gb():
    s = os.statvfs(ROOT)
    return int(s.f_bavail * s.f_frsize / 1e9)


if __name__ == "__main__":
    # corpus_record.py <repo> <binary> <test> <user_mark> <src_root>
    r = record_test(*sys.argv[1:6])
    print(json.dumps(r) if r else "FAILED")
