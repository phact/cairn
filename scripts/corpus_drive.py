#!/usr/bin/env python3
"""
Auto-driver for the corpus campaign: take one crate target, clone it, build all
its test binaries, auto-discover what got built, record a sibling group from
each, prune. Tolerant — any crate that fails is logged and skipped.

  corpus_drive.py <crate-name>          # drive one target from corpus_targets.json
  corpus_drive.py --next <N>            # drive the next N not-yet-done targets

Sibling preference: within a test binary we group tests by their top module
prefix and record the largest group (up to PER_GROUP) — same code path, diverse
inputs = the divergence that makes free diff labels.
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import corpus_record as CR        # still used for disk_free_gb
import xray_record as XR          # the FAST capture path

REPOS = os.path.join(ROOT, ".cairn", "corpus", "_repos")
TARGETS = os.path.join(ROOT, "scripts", "corpus_targets.json")
# "Executable <descriptor> (path)": descriptor is "tests/foo.rs" (integration)
# or "unittests src/lib.rs" (the crate's own unit tests).
EXEC_RE = re.compile(r"Executable (.+?) \(target/\S+/deps/[^)]+\)")
PER_GROUP = 4          # tests recorded per chosen sibling group
MAX_BINS = 4           # test targets per crate (xray runs are sub-second; afford more)
MIN_DISK_GB = 200      # safety floor


def _load():
    return json.load(open(TARGETS))


def _save(cfg):
    json.dump(cfg, open(TARGETS, "w"), indent=2)


def clone(t):
    d = os.path.join(REPOS, t["name"])
    if not os.path.isdir(d):
        r = subprocess.run(["git", "clone", "--depth", "1", t["git"], d],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return None
    return d


# trybuild/compile-fail shims build nothing runnable and break under xray
JUNK = {"compiletest", "ui", "derive_ui", "derive_fail", "trybuild", "macros",
        "compile_fail", "compiletests", "version-numbers", "ui_tests"}
# fuzz/property/bench targets produce unboundedly large xray logs — skip
JUNK_SUBSTR = ("fuzz", "bench", "quickcheck", "proptest")


def discover_targets(repo):
    """`cargo metadata` maps every integration-test target to its OWNING package
    — needed because many crates are workspaces with a virtual root manifest, so
    `cargo rustc --test X` must be scoped with `-p <pkg>`. Returns list of
    (pkg, spec, label)."""
    try:
        meta = json.loads(subprocess.check_output(
            ["cargo", "metadata", "--no-deps", "--format-version", "1"],
            cwd=repo, text=True, timeout=300, stderr=subprocess.DEVNULL))
    except Exception:
        return []
    integ, libs = [], []
    for pkg in meta.get("packages", []):
        mp = pkg["manifest_path"]          # unambiguous package selector
        for tgt in pkg.get("targets", []):
            kinds, name = tgt.get("kind", []), tgt.get("name", "")
            if ("test" in kinds and name not in JUNK
                    and not any(s in name for s in JUNK_SUBSTR)):   # integration
                integ.append((mp, ["--test", name], f"{name}"))
            elif "lib" in kinds:                            # lib unit tests
                libs.append((mp, ["--lib"], "lib"))
    # prefer integration tests; fall back to lib unit tests if none
    out = integ if integ else libs
    seen, dedup = set(), []
    for item in out:
        if item[2] not in seen:
            seen.add(item[2]); dedup.append(item)
    return dedup


def list_tests(binary):
    try:
        out = subprocess.check_output([binary, "--list"], text=True,
                                      stderr=subprocess.DEVNULL, timeout=120)
    except Exception:
        return []
    return [l.split(":")[0] for l in out.splitlines() if l.endswith(": test")]


def pick_group(tests):
    """Largest top-module sibling group, capped at PER_GROUP."""
    if not tests:
        return []
    groups = {}
    for t in tests:
        key = t.rsplit("::", 1)[0] if "::" in t else "_root"
        groups.setdefault(key, []).append(t)
    best = max(groups.values(), key=len)
    return best[:PER_GROUP]


def drive_one(t):
    name, mark = t["name"], t["mark"]
    print(f"### {name} (mark={mark})")
    if CR.disk_free_gb() < MIN_DISK_GB:
        print("  ABORT: low disk"); return False
    repo = clone(t)
    if not repo:
        print("  clone failed"); return False
    targets = discover_targets(repo)
    print(f"  {len(targets)} test targets: {[l for _, _, l in targets][:6]}")
    recorded, seen = 0, set()
    for manifest, spec, label in targets[:MAX_BINS]:
        binary = XR.build_xray(repo, manifest, spec)  # instrumented, non-PIE
        if not binary:
            print(f"    {label}: xray-build failed"); continue
        tests = list_tests(binary)
        group = [x for x in pick_group(tests) if x not in seen]
        duds = 0
        for test in group:
            seen.add(test)
            r = XR.record_test(name, binary, test, mark)     # native xray run
            tag = f"{r['frames']}f/{r['fns']}fn" if r else "FAIL"
            print(f"    {label} {test[:40]:42} {tag}")
            if r and r["frames"] >= 5:
                recorded += 1
            else:
                duds += 1
                if duds >= 2 and recorded == 0:
                    print("    (all duds — skipping rest of target)")
                    break
    return recorded > 0


def main():
    cfg = _load()
    if sys.argv[1] == "--next":
        n = int(sys.argv[2])
        todo = [t for t in cfg["targets"] if not t.get("done")][:n]
    else:
        todo = [t for t in cfg["targets"] if t["name"] == sys.argv[1]]
    for t in todo:
        ok = False
        try:
            ok = drive_one(t)
        except Exception as e:
            print(f"  EXC {type(e).__name__}: {e}")
        t["done"] = True          # attempted — don't retry failures every batch
        t["status"] = "ok" if ok else "failed"
        _save(cfg)        # checkpoint after each crate
    print(f"free disk: {CR.disk_free_gb()}GB")


if __name__ == "__main__":
    main()
