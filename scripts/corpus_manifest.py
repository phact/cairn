#!/usr/bin/env python3
"""Rebuild .cairn/corpus/manifest.json by scanning what's actually on disk.

Each test dir with a frames.jsonl is one corpus trace. A "repo" is a top-level
dir under corpus/. We track distinct repos toward the 100 target, plus frame
totals and current free disk. Derived-from-disk so it can't drift from reality.
"""
import json
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, ".cairn", "corpus")
MAN = os.path.join(CORPUS, "manifest.json")
MIN_FRAMES = 5          # below this = a dud (unit test out of scope); don't count


def scan():
    repos = {}
    for repo in sorted(os.listdir(CORPUS)):
        rd = os.path.join(CORPUS, repo)
        if not os.path.isdir(rd) or repo == "recordings":
            continue
        tests = []
        for t in sorted(os.listdir(rd)):
            fp = os.path.join(rd, t, "frames.jsonl")
            if not os.path.exists(fp):
                continue
            n = sum(1 for _ in open(fp))
            rp = fp + ".returns"
            rets = 0
            if os.path.exists(rp):
                try: rets = len(json.load(open(rp)))
                except Exception: pass
            tests.append({"test": t, "frames": n, "returns": rets,
                          "usable": n >= MIN_FRAMES})
        if tests:
            repos[repo] = tests
    return repos


def main():
    repos = scan()
    free = shutil.disk_usage(ROOT).free // (1024**3)
    prev = {}
    if os.path.exists(MAN):
        try: prev = json.load(open(MAN))
        except Exception: pass
    usable_traces = sum(1 for ts in repos.values() for t in ts if t["usable"])
    total_frames = sum(t["frames"] for ts in repos.values() for t in ts)
    # honest coverage = repos with >=1 USABLE trace (not dirs with only duds)
    usable_repos = sum(1 for ts in repos.values() if any(t["usable"] for t in ts))
    man = {
        "target_repos": prev.get("target_repos", 100),
        "disk_gb_start": prev.get("disk_gb_start", 2999),
        "disk_gb_now": free,
        "repos_covered": usable_repos,
        "dirs_total": len(repos),
        "usable_traces": usable_traces,
        "total_frames": total_frames,
        "repos": repos,
    }
    json.dump(man, open(MAN, "w"), indent=2)
    print(f"repos(usable)={usable_repos}/{man['target_repos']}  "
          f"usable_traces={usable_traces}  frames={total_frames}  free={free}GB")
    for r, ts in repos.items():
        u = sum(1 for t in ts if t["usable"])
        print(f"  {r:24} {u}/{len(ts)} usable  "
              f"frames={sum(t['frames'] for t in ts)}")


if __name__ == "__main__":
    main()
