#!/usr/bin/env python3
"""Bulk-record MANY tests per cloned repo to grow the UNLABELED pretraining
bucket (no diffs needed). Reuses the XRay capture path. Records a broad spread
of tests per target (not just sibling groups), skipping ones already recorded."""
import os, sys, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import corpus_drive as CD
import xray_record as XR
REPOS = os.path.join(ROOT, ".cairn", "corpus", "_repos")
PER_TARGET = 120         # capture full test suites for pretraining scale
MAX_BINS = 3
marks = {t["name"]: t["mark"] for t in json.load(open(os.path.join(ROOT, "scripts", "corpus_targets.json")))["targets"]}


def usable_in(repo):
    rd = os.path.join(ROOT, ".cairn", "corpus", repo)
    if not os.path.isdir(rd):
        return 0
    return sum(1 for t in os.listdir(rd)
               if os.path.exists(os.path.join(rd, t, "frames.jsonl"))
               and sum(1 for _ in open(os.path.join(rd, t, "frames.jsonl"))) >= 5)


def main():
    repos = sorted(os.listdir(REPOS))
    if len(sys.argv) > 1:
        repos = sys.argv[1:]
    for repo in repos:
        rd = os.path.join(REPOS, repo)
        if not os.path.isdir(rd):
            continue
        mark = marks.get(repo, repo.replace("-", "_") + "::")
        try:
            targets = CD.discover_targets(rd)
        except Exception:
            continue
        rec = 0
        for manifest, spec, label in targets[:MAX_BINS]:
            b = XR.build_xray(rd, manifest, spec)
            if not b:
                continue
            tests = CD.list_tests(b)
            step = max(1, len(tests) // PER_TARGET)
            for t in tests[::step][:PER_TARGET]:
                dirn = t.replace("::", "_").replace("/", "_")
                if os.path.exists(os.path.join(ROOT, ".cairn", "corpus", repo, dirn, "frames.jsonl")):
                    continue
                r = XR.record_test(repo, b, t, mark)
                if r and r["frames"] >= 5:
                    rec += 1
        print(f"{repo}: +{rec} usable (now {usable_in(repo)})", flush=True)
    print("BULKDONE", flush=True)


if __name__ == "__main__":
    main()
