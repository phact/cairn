#!/usr/bin/env python3
"""Backfill provenance for every corpus repo: the git remote + the exact commit
the traces were recorded at (read from the clone that's still on disk), plus the
test list. Writes corpus/<repo>/meta.json. Run before the clones drift."""
import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, ".cairn", "corpus")
REPOS = os.path.join(CORPUS, "_repos")
TARGETS = os.path.join(ROOT, "scripts", "corpus_targets.json")


def git(clone, *args):
    try:
        return subprocess.check_output(["git", "-C", clone, *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def main():
    gitof = {t["name"]: t.get("git", "") for t in
             json.load(open(TARGETS))["targets"]}
    wrote = 0
    for repo in sorted(os.listdir(CORPUS)):
        rd = os.path.join(CORPUS, repo)
        if not os.path.isdir(rd) or repo in ("_repos", "recordings"):
            continue
        tests = [t for t in os.listdir(rd)
                 if os.path.exists(os.path.join(rd, t, "frames.jsonl"))]
        if not tests:
            continue
        clone = os.path.join(REPOS, repo)
        commit = git(clone, "rev-parse", "HEAD")
        remote = git(clone, "remote", "get-url", "origin") or gitof.get(repo)
        meta = {
            "repo": repo,
            "git": remote,
            "commit": commit,                 # None if clone was pruned
            "commit_recovered": commit is not None,
            "tests": sorted(tests),
            "n_traces": len(tests),
        }
        json.dump(meta, open(os.path.join(rd, "meta.json"), "w"), indent=2)
        wrote += 1
    missing = sum(1 for repo in os.listdir(CORPUS)
                  if os.path.isdir(os.path.join(CORPUS, repo))
                  and os.path.exists(os.path.join(CORPUS, repo, "meta.json"))
                  and json.load(open(os.path.join(CORPUS, repo, "meta.json")))
                          ["commit"] is None)
    print(f"wrote meta.json for {wrote} repos; {missing} missing a commit (clone gone)")


if __name__ == "__main__":
    main()
