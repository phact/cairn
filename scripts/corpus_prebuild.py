#!/usr/bin/env python3
"""Pipelining helper: clone + `cargo test --no-run` for pending corpus targets,
so the recording driver never blocks on a cold build. Walks the pending list in
REVERSE (the driver consumes it forward via --next), so the two processes work
opposite ends and rarely contend on the same crate's target dir.

Only clones + builds — never writes corpus_targets.json or the corpus — so it
can't race the driver's checkpoint. Idempotent (cargo skips up-to-date builds).
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import corpus_drive as D


def main():
    cfg = json.load(open(D.TARGETS))
    pending = [t for t in cfg["targets"] if not t.get("done")]
    for t in reversed(pending):
        repo = os.path.join(D.REPOS, t["name"])
        try:
            r = D.clone(t)
            if not r:
                print(f"  {t['name']:18} clone-failed", flush=True); continue
            bins = D.build_tests(r)
            print(f"  {t['name']:18} built {len(bins)} bins", flush=True)
        except Exception as e:
            print(f"  {t['name']:18} {type(e).__name__}", flush=True)
    print("prebuild done", flush=True)


if __name__ == "__main__":
    main()
