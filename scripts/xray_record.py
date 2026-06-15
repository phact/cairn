#!/usr/bin/env python3
"""
XRay-based corpus capture — the FAST path (replaces the gdb breakpoint-replay
extractor for the corpus campaign).

Per test target:
  cargo +nightly rustc --test <t>  -Zinstrument-xray=always (RUSTFLAGS, all deps)
    + final-binary link: map_dump.o, -no-pie, whole-archive xray runtime
  -> instrumented, non-PIE test binary  (built ONCE per target)
Per test:
  run the binary NATIVELY with XRAY basic-mode logging  (sub-second, real
    execution — every fn entry/exit logged at native speed)
  -> xray.structure() reconstructs the FrameStream in ~tens of ms

No rr, no gdb, no per-frame breakpoints. Structure only (the cheap pretraining
bucket); arms are a separate bounded pass when labels are needed.
"""
import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backends", "rust"))
import xray

CORPUS = os.path.join(ROOT, ".cairn", "corpus")
MAP_O = os.path.join(ROOT, ".cairn", "xray", "map_dump.o")


def _xray_libs():
    """Newest system compiler-rt xray runtime (basic + core)."""
    cands = sorted(glob.glob("/usr/lib/llvm-*/lib/clang/*/lib/linux/libclang_rt.xray-x86_64.a"))
    if not cands:
        return None
    core = cands[-1]
    basic = core.replace("xray-x86_64", "xray-basic-x86_64")
    return (core, basic) if os.path.exists(basic) else None


def _newest_bin(repo, name_hint):
    """Locate the freshly-built test binary in deps/. cargo rustc (unlike
    `cargo test --no-run`) prints no Executable line, so we glob: prefer
    <name_hint>-<hash>, else newest executable in deps."""
    deps = os.path.join(repo, "target", "debug", "deps")
    if not os.path.isdir(deps):
        return None
    cands = []
    for f in os.listdir(deps):
        p = os.path.join(deps, f)
        if "." in f or not os.path.isfile(p) or not os.access(p, os.X_OK):
            continue
        if name_hint and not f.startswith(name_hint + "-"):
            continue
        cands.append(p)
    return max(cands, key=os.path.getmtime) if cands else None


def build_xray(repo, manifest, target_spec, timeout=900):
    """target_spec: ['--test','stream'] or ['--lib']; `manifest` is the owning
    package's Cargo.toml (selects it unambiguously — `-p name` is ambiguous when
    the crate is also a registry dep of a sibling). Returns binary path|None.

    Instruments all deps via RUSTFLAGS; puts the xray runtime + map-dumper +
    -no-pie on the final test binary via `cargo rustc -- <linkargs>` (keeps the
    relocation-model/link flags off proc-macro dylibs)."""
    libs = _xray_libs()
    if not libs or not os.path.exists(MAP_O):
        return None
    core, basic = libs
    link = ["-Crelocation-model=static",
            f"-Clink-arg={MAP_O}", "-Clink-arg=-no-pie",
            "-Clink-arg=-Wl,--whole-archive",
            f"-Clink-arg={core}", f"-Clink-arg={basic}",
            "-Clink-arg=-Wl,--no-whole-archive",
            "-Clink-arg=-lstdc++", "-Clink-arg=-lpthread", "-Clink-arg=-ldl"]
    env = {**os.environ, "RUSTFLAGS": "-Zinstrument-xray=always",
           "CARGO_TERM_COLOR": "never"}
    # For lib unit tests, `cargo rustc --lib` builds the rlib, NOT a runnable
    # test binary — pass `--test` to rustc so it compiles the #[test] harness.
    rustc_flags = (["--test"] + link) if target_spec[0] == "--lib" else link
    cmd = (["cargo", "+nightly", "rustc", "--manifest-path", manifest]
           + target_spec + ["-j8", "--"] + rustc_flags)
    try:
        r = subprocess.run(cmd, cwd=repo, env=env, capture_output=True,
                           text=True, timeout=timeout)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    hint = target_spec[1] if target_spec[0] == "--test" else None
    return _newest_bin(repo, hint)


def record_test(repo_name, binary, test, crate_mark, xtmp="/tmp/cairn_xr"):
    """Native instrumented run of one test -> frames.jsonl via xray.structure."""
    import json
    out = os.path.join(CORPUS, repo_name, test.replace("::", "_").replace("/", "_"))
    os.makedirs(out, exist_ok=True)
    shutil.rmtree(xtmp, ignore_errors=True); os.makedirs(xtmp)
    env = {**os.environ,
           "XRAY_OPTIONS": f"patch_premain=true xray_mode=xray-basic xray_logfile_base={xtmp}/log.",
           "XRAY_BASIC_OPTIONS": "func_duration_threshold_us=0",
           "CAIRN_XRAY_MAP": f"{xtmp}/map"}
    # run from the crate's repo root (tests may read fixtures relative to cwd)
    cwd = binary.split("/target/")[0] if "/target/" in binary else ROOT
    try:
        subprocess.run([binary, "--exact", test, "--test-threads=1", "--nocapture"],
                       env=env, cwd=cwd, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    logs = glob.glob(f"{xtmp}/log.*")
    if not logs or not os.path.exists(f"{xtmp}/map"):
        return None
    # Fuzz/property tests run millions of iterations -> multi-GB xray log; reading
    # it whole would OOM-kill the process. Cap: a real "trace" is bounded.
    if os.path.getsize(logs[0]) > 250 * 1024 * 1024:
        return None
    # Fence to the crate's OWN source tree so std/alloc generics monomorphized
    # with this crate's types (their mangled name carries the crate, so the
    # symbol filter alone lets them through) don't leak in. The crate's files
    # live under its checkout; std is /rustc/.., deps are /.cargo/registry/..
    repo = binary.split("/target/")[0]
    xray.FILE_MARK = repo + "/"
    try:
        frames, counts = xray.structure(logs[0], f"{xtmp}/map", binary,
                                        crate_mark.split("::")[0].replace("-", "_"))
    except Exception:
        return None
    fp = os.path.join(out, "frames.jsonl")
    with open(fp, "w") as f:
        for fr in frames:
            f.write(json.dumps(fr) + "\n")
    json.dump(counts, open(fp + ".counts", "w"))
    return {"frames": len(frames), "fns": len(counts), "out": out}
