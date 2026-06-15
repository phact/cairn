#!/usr/bin/env python3
"""
Arms pass — add value arms (.returns) to corpus traces that only have XRay
structure, so sibling DIFFS can find flips (the labeled bucket).

XRay logs entry/exit but no values; return arms (Ok/Err/Some/None/…) require a
debugger stop. So for the SIBLING-GROUP tests of each repo we run the existing
gdb extractor in direct-run mode (CAIRN_MODE=gdb) on the already-built test
binary, breaking only on the crate's own functions and capturing each call's
return via FinishBP. We keep the XRay frames.jsonl and drop the gdb-derived
.returns beside it (flip detection reads .returns only; both runs are
deterministic so the arm sequences correspond).

This is bounded to the diffed sibling tests, not all 450 traces — values only
come from gdb, so the labeled bucket is necessarily the slower path.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, ".cairn", "corpus")
REPOS = os.path.join(CORPUS, "_repos")
EXTRACT = os.path.join(ROOT, "backends", "rust", "extract.py")
TARGETS = os.path.join(ROOT, "scripts", "corpus_targets.json")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import corpus_diff
import corpus_drive as CD          # discover_targets / list_tests / MAX_BINS
import xray_record as XR           # build_xray (cached -> the same instrumented bin)


def _rust_env():
    try:
        sr = subprocess.check_output(["rustc", "--print", "sysroot"], text=True).strip()
        return {"CAIRN_RUST_SYSROOT": sr,
                "CAIRN_RUST_ETC": os.path.join(sr, "lib", "rustlib", "etc")}
    except Exception:
        return {}


def test_binaries(repo_dir):
    """test-binary path -> set(test names), using the SAME discovery+build the
    XRay capture used (cargo metadata -> `cargo rustc` per target). This reuses
    the already-built instrumented binaries (so the test names match what was
    recorded) and avoids a fresh `cargo test --no-run`, which fails on crates
    whose integration-test dev-deps don't resolve (e.g. bitvec's num-traits pin)
    or whose usable traces were lib unit tests."""
    bins = {}
    for manifest, spec, label in CD.discover_targets(repo_dir)[:CD.MAX_BINS]:
        b = XR.build_xray(repo_dir, manifest, spec)
        if b:
            bins[b] = set(CD.list_tests(b))
    return bins


def arms_for_test(repo, dirname, binary, real_test, mark, timeout=240):
    """Run gdb on `binary` for `real_test`, write .returns next to the corpus
    trace's frames.jsonl. mark with '::' -> name scope, else file scope."""
    out_dir = os.path.join(CORPUS, repo, dirname)
    tmp = os.path.join("/tmp", "cairn_arms", repo, dirname)
    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp, exist_ok=True)
    name_mark = mark if "::" in mark else mark        # bare mark also name-scopes
    env = {**os.environ, **_rust_env(),
           "CAIRN_MODE": "gdb", "CAIRN_TEST": real_test,
           "CAIRN_OUT": os.path.join(tmp, "frames.jsonl"),
           "CAIRN_NAME_MARK": name_mark, "CAIRN_USER_MARK": "", "CAIRN_FILES": "",
           "CAIRN_ARMS_ONCE": "1",       # bounded: one stop per function
           # the binary is XRay-instrumented; satisfy its map-dumper constructor
           "CAIRN_XRAY_MAP": os.path.join(tmp, "xmap")}
    try:
        subprocess.run(["gdb", "-q", "-batch", "-x", EXTRACT, binary], env=env,
                       cwd=os.path.dirname(binary).split("/target/")[0],
                       stdin=subprocess.DEVNULL, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    rp = os.path.join(tmp, "frames.jsonl.returns")
    if os.path.exists(rp) and os.path.getsize(rp) > 2:
        shutil.copy(rp, os.path.join(out_dir, "frames.jsonl.returns"))
        return True
    return False


def _real_test_name(dirname, all_tests):
    """The corpus dir name is the test with :: -> _ and / -> _. Recover the
    actual test id by matching against the binary's real test list."""
    if dirname in all_tests:
        return dirname
    norm = {t.replace("::", "_").replace("/", "_"): t for t in all_tests}
    return norm.get(dirname)


def arms_repo(repo, mark):
    """Ensure every sibling-group test in `repo` has arms."""
    groups = corpus_diff.sibling_groups(repo)
    want = {t for g in groups for t in g}
    # skip those that already have arms
    want = {t for t in want if not os.path.exists(
        os.path.join(CORPUS, repo, t, "frames.jsonl.returns"))}
    if not want:
        return 0
    repo_dir = os.path.join(REPOS, repo)
    if not os.path.isdir(repo_dir):
        return 0
    bins = test_binaries(repo_dir)
    done = 0
    for dirname in sorted(want):
        for binary, tests in bins.items():
            real = _real_test_name(dirname, tests)
            if real:
                if arms_for_test(repo, dirname, binary, real, mark):
                    done += 1
                break
    return done


def main():
    marks = {t["name"]: t["mark"] for t in json.load(open(TARGETS))["targets"]}
    repos = sys.argv[1:] or sorted(
        r for r in os.listdir(CORPUS)
        if os.path.isdir(os.path.join(CORPUS, r)) and r not in ("_repos", "recordings"))
    for repo in repos:
        mark = marks.get(repo, repo + "::")
        try:
            n = arms_repo(repo, mark)
        except Exception as e:
            print(f"{repo}: EXC {type(e).__name__}: {e}", flush=True); continue
        d = corpus_diff.diff_repo(repo)
        print(f"{repo}: +{n} arms  ->  {d['n_pairs']} pairs, "
              f"{d['total_deciders']} deciders, arms={d['any_arms']}", flush=True)


if __name__ == "__main__":
    main()
