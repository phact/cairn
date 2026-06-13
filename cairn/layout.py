"""
Cairn on-disk layout — the single source of path truth.

    .cairn/
      config.toml            active_trace pointer (the session state a CLI can't hold)
      predictions.jsonl      PERSISTENT, CROSS-TRACE — the only non-regenerable state
      traces/
        <trace-id>/
          recording/         rr recording — the SINGLE SOURCE OF TRUTH
          derived/           rm -rf safe; rebuilds from recording/
            artifact.json    the served artifact (contract with UI + CLI + MCP)
            frames_rr.jsonl  (+ .counts / .returns)
            loc_cache/
            top20.txt

Two boundaries govern it: lifecycle (regenerable derived/ vs the truth in
recording/) and identity (per-trace derived state vs the user-global, cross-trace
predictions log that keys on frame_kind so the frontier model generalizes).
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def home():
    return os.environ.get("CAIRN_HOME", os.path.join(ROOT, ".cairn"))


def config_path():
    return os.path.join(home(), "config.toml")


def predictions_path():
    return os.path.join(home(), "predictions.jsonl")


def traces_dir():
    return os.path.join(home(), "traces")


def trace_dir(tid):
    return os.path.join(traces_dir(), tid)


def recording_dir(tid):
    return os.path.join(trace_dir(tid), "recording")


def rr_trace(tid):
    """The actual rr trace directory inside recording/ (rr names it after the
    binary). The one holding rr's `version` marker."""
    rec = recording_dir(tid)
    if not os.path.isdir(rec):
        return None
    for name in sorted(os.listdir(rec)):
        d = os.path.join(rec, name)
        # skip rr's `latest-trace` symlink — we want the real trace dir
        if (os.path.isdir(d) and not os.path.islink(d)
                and os.path.exists(os.path.join(d, "version"))):
            return d
    return None


def derived_dir(tid):
    return os.path.join(trace_dir(tid), "derived")


def artifact_path(tid):
    return os.path.join(derived_dir(tid), "artifact.json")


def frames_path(tid):
    return os.path.join(derived_dir(tid), "frames_rr.jsonl")


def loc_cache_dir(tid):
    return os.path.join(derived_dir(tid), "loc_cache")


def top20_path(tid):
    return os.path.join(derived_dir(tid), "top20.txt")


def list_traces():
    td = traces_dir()
    if not os.path.isdir(td):
        return []
    return sorted(d for d in os.listdir(td) if os.path.isdir(trace_dir(d)))


def read_active():
    p = config_path()
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line.startswith("active_trace"):
                return line.split("=", 1)[1].strip().strip('"')
    return None


def write_active(tid):
    os.makedirs(home(), exist_ok=True)
    with open(config_path(), "w") as fh:
        fh.write(f'active_trace = "{tid}"\n')


def resolve_active():
    """Active trace from config; else the sole trace if exactly one exists."""
    a = read_active()
    if a:
        return a
    traces = list_traces()
    return traces[0] if len(traces) == 1 else None


def project_path():
    return os.path.join(home(), "project.toml")


_PROJECT = None


def project_config():
    """Per-project capture settings, kept OUT of the committed source so the
    engine stays scenario-agnostic. `.cairn/project.toml` (gitignored) sets the
    binary, crate, default test, and the user-code scope (user_mark / files /
    strip_prefix) for the project being recorded. Absent keys fall back to the
    generic defaults baked into the engine. Simple `key = "value"` lines."""
    global _PROJECT
    if _PROJECT is None:
        _PROJECT = {}
        p = project_path()
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                _PROJECT[k.strip()] = v.strip().strip('"')
    return dict(_PROJECT)
