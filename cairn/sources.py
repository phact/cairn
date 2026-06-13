"""Source-path resolution (core). A trace records file paths as the runtime
saw them (relative crate paths for Rust DWARF, absolute for Python). Resolving
them to readable files is a core concern — every backend and the source
endpoints need it — parameterized by CAIRN_SRC_ROOT for relative paths."""

import os

import layout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Where the recorded program's sources live, for resolving relative crate paths.
# Supplied by env or the gitignored project config; the engine names no repo.
SRC_ROOT = os.environ.get("CAIRN_SRC_ROOT") or \
    layout.project_config().get("src_root", "")
# anchor a relative root to the repo, not the caller's cwd (subprocesses vary)
if SRC_ROOT and not os.path.isabs(SRC_ROOT):
    SRC_ROOT = os.path.normpath(os.path.join(ROOT, SRC_ROOT))


def resolve(rel_or_abs):
    """Absolute, existing path for a recorded source path — or None (the
    caller reports 'source unavailable'; it never fabricates content)."""
    if os.path.isabs(rel_or_abs) and os.path.exists(rel_or_abs):
        return rel_or_abs
    if not SRC_ROOT:
        return None
    cand = os.path.normpath(os.path.join(SRC_ROOT, rel_or_abs))
    return cand if os.path.exists(cand) else None
