"""
cairn capture-on-fail — record the bug while it exists.

Wraps a test invocation (`cairn capture-on-fail -- cargo test …`). While tests
pass it is a transparent pass-through with ~zero overhead. When tests fail, it
re-runs each failing test under `rr record`, derives a trace tagged
`fail-<sha>::<test>@red`, and prints the commands to interrogate it. The
wrapped command's exit code is preserved either way (CI still goes red).

Honesty rules baked in:
  * If the isolated rr re-run PASSES, the failure didn't reproduce — the trace
    is tagged `@flaky`, never presented as the bug's recording.
  * If rr produces no recording, we say so; nothing is synthesized.

`capture_fix(red_tid)` records the green side after a fix lands (same test,
same crate prefix), so `cairn diff` of the pair is the bug's flip signature.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys

import layout
import pipeline

ROOT = layout.ROOT

# cargo/libtest output shapes
RUN_RE = re.compile(r"^\s*Running (?:unittests )?\S+ \((\S+)\)\s*$")
FAIL_RE = re.compile(r"^test (\S+) \.\.\. FAILED\s*$")


def _sha(cwd):
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=cwd, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "local"


def run_wrapped(cmd, cwd):
    """Run the test command, streaming output through unchanged while mapping
    failing tests to the binary they ran from. Returns (exit_code, failures)
    with failures = [(binary_path, test_name), ...] in first-seen order."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    failures, seen = [], set()
    current_bin = None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        m = RUN_RE.match(line)
        if m:
            current_bin = m.group(1)
            continue
        m = FAIL_RE.match(line)
        if m and current_bin and (current_bin, m.group(1)) not in seen:
            seen.add((current_bin, m.group(1)))
            failures.append((current_bin, m.group(1)))
    proc.wait()
    return proc.returncode, failures


def record_failure(binary, test, sha, user_mark=None, files=None, log=print):
    """rr-record one failing test in isolation. Tag @red if it failed under rr
    too, @flaky if it passed in isolation (didn't reproduce — also signal, but
    never presented as the bug's recording). Returns the trace id or None."""
    binary = os.path.abspath(binary)
    if user_mark:
        os.environ["CAIRN_USER_MARK"] = user_mark
    if files:
        os.environ["CAIRN_FILES"] = files
    leaf = test.split("::")[-1]
    tid = f"fail-{sha}::{leaf}@red"
    shutil.rmtree(layout.trace_dir(tid), ignore_errors=True)
    rec = layout.recording_dir(tid)
    os.makedirs(rec, exist_ok=True)
    log(f"[capture-on-fail] recording {test} under rr …")
    r = subprocess.run(["rr", "record", binary, "--", "--exact", test,
                        "--test-threads=1", "--nocapture"],
                       cwd=ROOT, env={**os.environ, "_RR_TRACE_DIR": rec},
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if layout.rr_trace(tid) is None:
        log(f"[capture-on-fail] rr produced no recording for {test} — skipped")
        shutil.rmtree(layout.trace_dir(tid), ignore_errors=True)
        return None
    if r.returncode == 0:
        # didn't reproduce in isolation — rename honestly
        flaky = f"fail-{sha}::{leaf}@flaky"
        shutil.rmtree(layout.trace_dir(flaky), ignore_errors=True)
        shutil.move(layout.trace_dir(tid), layout.trace_dir(flaky))
        tid = flaky
        log(f"[capture-on-fail] {test} PASSED in isolation — tagged @flaky "
            f"(the suite-context failure was not reproduced)")
    meta = {"test": test, "binary": binary,
            "bin_stem": os.path.basename(binary).rsplit("-", 1)[0],
            "sha": sha, "tag": tid.rsplit("@", 1)[1],
            # source-scope filters used, so capture_fix reproduces them
            "user_mark": os.environ.get("CAIRN_USER_MARK"),
            "files": os.environ.get("CAIRN_FILES")}
    with open(os.path.join(layout.recording_dir(tid), "capture_meta.json"), "w") as fh:
        json.dump(meta, fh)
    os.environ["CAIRN_BIN"] = binary
    pipeline.derive(tid, log=lambda *a: None)
    return tid


def capture_on_fail(cmd, cap=3, user_mark=None, files=None, log=print):
    """The wrapper. Returns the wrapped command's exit code."""
    cwd = os.getcwd()
    code, failures = run_wrapped(cmd, cwd)
    if code == 0:
        return 0
    if not failures:
        log(f"[capture-on-fail] command failed (exit {code}) but no test "
            f"failures were parsed — nothing recorded")
        return code
    sha = _sha(cwd)
    keep_active = layout.resolve_active()
    recorded = []
    try:
        for binary, test in failures[:cap]:
            tid = record_failure(binary, test, sha, user_mark, files, log=log)
            if tid:
                recorded.append(tid)
        if len(failures) > cap:
            log(f"[capture-on-fail] {len(failures) - cap} further failures "
                f"not recorded (--cap {cap})")
    finally:
        if keep_active:
            layout.write_active(keep_active)
    for tid in recorded:
        log(f"[capture-on-fail] recorded -> {tid}")
        log(f"    cairn skeleton --trace '{tid}'")
        log(f"    cairn explain 1 --trace '{tid}'")
    return code


def capture_fix(red_tid, binary=None, log=print):
    """Record the green side after the fix: same test, same crate prefix, so
    `cairn diff red green` is the bug's flip signature."""
    meta_path = os.path.join(layout.recording_dir(red_tid), "capture_meta.json")
    if not os.path.exists(meta_path):
        raise pipeline.PipelineError(f"no capture metadata for `{red_tid}`")
    meta = json.load(open(meta_path))
    test = meta["test"]
    # reproduce the red side's source-scope filters so the green extract sees
    # the same user functions (else the diff has nothing to compare)
    if meta.get("user_mark"):
        os.environ["CAIRN_USER_MARK"] = meta["user_mark"]
    if meta.get("files"):
        os.environ["CAIRN_FILES"] = meta["files"]
    if binary is None:
        # the fix rebuilt the binary under a new hash; newest matching stem
        # that still lists the test wins
        pat = os.path.join(os.path.dirname(meta["binary"]),
                           meta["bin_stem"] + "-*")
        cands = sorted((p for p in glob.glob(pat)
                        if os.access(p, os.X_OK) and not p.endswith(".d")),
                       key=os.path.getmtime, reverse=True)
        for c in cands:
            ls = subprocess.run([c, "--list"], capture_output=True, text=True)
            if test.split("::")[-1] in ls.stdout:
                binary = c
                break
        if binary is None:
            raise pipeline.PipelineError(
                f"could not locate a rebuilt binary for `{meta['bin_stem']}` "
                f"containing {test}; pass --bin")
    crate = red_tid.split("::", 1)[0]
    leaf = test.split("::")[-1]
    tid = f"{crate}::{leaf}@green"
    shutil.rmtree(layout.trace_dir(tid), ignore_errors=True)
    rec = layout.recording_dir(tid)
    os.makedirs(rec, exist_ok=True)
    log(f"[capture-fix] recording {test} at the fix …")
    r = subprocess.run(["rr", "record", os.path.abspath(binary), "--",
                        "--exact", test, "--test-threads=1", "--nocapture"],
                       cwd=ROOT, env={**os.environ, "_RR_TRACE_DIR": rec},
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        log(f"[capture-fix] WARNING: {test} still FAILS — this is another red "
            f"run, not a fix; tagging @still-red")
        tid2 = f"{crate}::{leaf}@still-red"
        shutil.rmtree(layout.trace_dir(tid2), ignore_errors=True)
        shutil.move(layout.trace_dir(tid), layout.trace_dir(tid2))
        tid = tid2
    keep_active = layout.resolve_active()
    os.environ["CAIRN_BIN"] = os.path.abspath(binary)
    try:
        pipeline.derive(tid, log=lambda *a: None)
    finally:
        if keep_active:
            layout.write_active(keep_active)
    log(f"[capture-fix] recorded -> {tid}")
    if tid.endswith("@green"):
        log(f"    cairn --trace '{red_tid}' diff '{tid}'   # the bug's flip signature")
    return tid
