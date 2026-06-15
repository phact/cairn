#!/usr/bin/env python3
"""
Thicken thin repos: a repo with <2 usable traces can't form a sibling pair, so
no diff. Record MORE tests for it via the fast XRay path (broadly across its
targets, not just one sibling group) until it has >=2 substantial traces.

Some crates are genuinely thin (only trivial/inlined tests) and stay <2 — those
remain structure-only and are reported, not forced.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, ".cairn", "corpus")
REPOS = os.path.join(CORPUS, "_repos")
TARGETS = os.path.join(ROOT, "scripts", "corpus_targets.json")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import corpus_drive as CD
import xray_record as XR

TARGET_USABLE = 3        # stop once a repo has this many usable traces
PER_TARGET = 10          # tests to try per target binary
MIN_FRAMES = 5


def usable(repo):
    rd = os.path.join(CORPUS, repo)
    if not os.path.isdir(rd):
        return 0
    n = 0
    for t in os.listdir(rd):
        fp = os.path.join(rd, t, "frames.jsonl")
        if os.path.exists(fp) and sum(1 for _ in open(fp)) >= MIN_FRAMES:
            n += 1
    return n


def thicken(repo, mark):
    repo_dir = os.path.join(REPOS, repo)
    if not os.path.isdir(repo_dir):
        return usable(repo)
    targets = CD.discover_targets(repo_dir)
    for manifest, spec, label in targets[:CD.MAX_BINS]:
        if usable(repo) >= TARGET_USABLE:
            break
        binary = XR.build_xray(repo_dir, manifest, spec)
        if not binary:
            continue
        tests = CD.list_tests(binary)
        # spread across the test list (every Nth) to dodge a dud-heavy prefix
        step = max(1, len(tests) // PER_TARGET)
        for t in tests[::step][:PER_TARGET]:
            dirn = t.replace("::", "_").replace("/", "_")
            if os.path.exists(os.path.join(CORPUS, repo, dirn, "frames.jsonl")):
                continue
            XR.record_test(repo, binary, t, mark)
            if usable(repo) >= TARGET_USABLE:
                break
    return usable(repo)


def main():
    marks = {t["name"]: t["mark"] for t in json.load(open(TARGETS))["targets"]}
    repos = sys.argv[1:]
    if not repos:
        repos = sorted(r for r in os.listdir(CORPUS)
                       if os.path.isdir(os.path.join(CORPUS, r))
                       and r not in ("_repos", "recordings")
                       and usable(r) < 2)
    for repo in repos:
        before = usable(repo)
        n = thicken(repo, marks.get(repo, repo + "::"))
        print(f"{repo}: {before} -> {n} usable", flush=True)


if __name__ == "__main__":
    main()
