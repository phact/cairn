#!/usr/bin/env python3
"""
Mine a commit pair into Cairn traces: record the SAME test on two builds of
the code, derive artifacts per side (against that side's checkout, so
source-dependent fields are version-correct), ready for `cairn diff`/`bench`.

Trace ids: pr-<label>::<test>@<commit>  — both sides share the crate prefix
`pr-<label>`, so bench pairs them with each other and nothing else.

Usage: mine_pair.py <label> <commitA> <commitB> [test]
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cairn"))
import layout            # noqa: E402
import pipeline          # noqa: E402

MINING = os.path.join(ROOT, ".cairn", "mining")
REPO = os.path.join(MINING, "repo")
_PROJ = layout.project_config()
DEFAULT_TEST = _PROJ.get("default_test", "")
DEFAULT_BIN_SUFFIX = os.path.basename(_PROJ.get("binary", "test"))


def mine_side(label, commit, test, bin_suffix=DEFAULT_BIN_SUFFIX,
              user_mark=None, files=None, allow_fail=False):
    short = test.split("::")[-1]
    tid = f"pr-{label}::{short}@{commit}"
    binpath = os.path.join(MINING, "bins", f"{commit}-{bin_suffix}")
    if not os.path.exists(binpath):
        raise SystemExit(f"no built binary for {commit}")
    if user_mark:
        os.environ["CAIRN_USER_MARK"] = user_mark
    if files:
        os.environ["CAIRN_FILES"] = files
    # checkout the side's source so derive reads version-correct files
    subprocess.run(["git", "-c", "advice.detachedHead=false", "checkout",
                    "--quiet", commit], cwd=REPO, check=True)
    os.environ["CAIRN_BIN"] = binpath
    os.environ["CAIRN_SRC_ROOT"] = REPO

    rec = layout.recording_dir(tid)
    import shutil
    shutil.rmtree(layout.trace_dir(tid), ignore_errors=True)
    os.makedirs(rec, exist_ok=True)
    print(f"[mine] recording {tid}")
    r = subprocess.run(["rr", "record", binpath, "--", "--exact", test,
                        "--test-threads=1", "--nocapture"],
                       check=False, cwd=ROOT,
                       env={**os.environ, "_RR_TRACE_DIR": rec},
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0 and not allow_fail:
        raise SystemExit(f"test failed at {commit} (use --allow-fail to mine "
                         f"a red run): exit {r.returncode}")
    if r.returncode != 0:
        print(f"[mine] note: test FAILED at {commit} (red run, recorded anyway)")
    pipeline.derive(tid, log=lambda *a: None)
    print(f"[mine] derived {tid}")
    return tid


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("label"); ap.add_argument("commit_a"); ap.add_argument("commit_b")
    ap.add_argument("--test", default=DEFAULT_TEST)
    ap.add_argument("--bin-suffix", default=DEFAULT_BIN_SUFFIX)
    ap.add_argument("--user-mark", default=None)
    ap.add_argument("--files", default=None)
    ap.add_argument("--allow-fail", action="store_true")
    a = ap.parse_args()
    keep_active = layout.resolve_active()
    try:
        ta = mine_side(a.label, a.commit_a, a.test, a.bin_suffix,
                       a.user_mark, a.files, a.allow_fail)
        tb = mine_side(a.label, a.commit_b, a.test, a.bin_suffix,
                       a.user_mark, a.files, a.allow_fail)
    finally:
        if keep_active:
            layout.write_active(keep_active)     # never disturb the live session
    print(f"[mine] pair ready: {ta}  ⇄  {tb}")


if __name__ == "__main__":
    main()
