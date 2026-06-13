"""
Capture-backend registry (core) — COMPOSITION, not inheritance.

Backends share the CONTRACT (FrameStream schema, artifact env, layout,
deref.status semantics — all defined in core) and diverge in every mechanism;
several mechanisms even live in other processes (a gdb-embedded agent, a
monitored target). So there is no base class to inherit from: each backend is
a plain object satisfying the `CaptureBackend` Protocol, registered by the
`capture_backend` id the artifact declares. Dispatch is a data lookup.

Adding a language = drop a module under backends/<lang>/, write one adapter
class here (or register from the module), declare honest capabilities.
"""

import os
import sys
from typing import Protocol

from errors import LocUnavailable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUST_DIR = os.path.join(ROOT, "backends", "rust")
_PY_DIR = os.path.join(ROOT, "backends", "python")
for _p in (_RUST_DIR, _PY_DIR):
    if _p not in sys.path:
        sys.path.append(_p)        # append: core modules win name collisions

import layout  # noqa: E402


class CaptureBackend(Protocol):
    """What the core needs from a backend. Structural typing only — no
    runtime inheritance; conformance is checked by type-checkers and tests."""
    id: str            # == artifact scenario.capture_backend
    language: str

    def capabilities(self) -> dict: ...
    def record(self, log=print, **params) -> str: ...          # -> trace id
    def extract(self, tid: str, log=print) -> None: ...        # recording -> frame stream
    def artifact_env(self, tid: str) -> dict: ...              # env for rank/artifact
    def loc(self, art, frame, instant=None) -> dict: ...       # tier 3
    def resolve_value(self, art, frame, path, instant=None, depth=5) -> dict: ...
    def warm(self, tid: str) -> str: ...                       # pin tier-3 session
    def precompute(self, art, log=print) -> int: ...           # warm loc cache


# ---------------------------------------------------------------------------
class RustRr:
    """rr + gdb + DWARF. Tier-3 is a LIVE warm replay session (time travel)."""
    id = "rr-dwarf"
    language = "rust"
    CAPS = {"reverse_step": True, "loc_state": True, "value_tree": True,
            "spans": False, "counterfactual": True}

    def _rrloc(self):
        import rrloc
        return rrloc

    def capabilities(self):
        return dict(self.CAPS)

    def record(self, log=print, **params):
        import pipeline
        return pipeline.record_rust(params["crate"], params["test"], log=log)

    def extract(self, tid, log=print):
        import pipeline
        pipeline.extract_rust(tid, log=log)

    def artifact_env(self, tid):
        import json
        crate = tid.split("::", 1)[0]
        return {"CAIRN_OUT": layout.frames_path(tid),
                "CAIRN_CRATE": crate, "CAIRN_SCENARIO_ID": tid,
                "CAIRN_LANGUAGE": self.language,
                "CAIRN_CAPTURE_BACKEND": self.id,
                "CAIRN_CAPABILITIES": json.dumps(self.CAPS),
                "CAIRN_ARTIFACT": layout.artifact_path(tid)}

    def loc(self, art, frame, instant=None):
        return self._rrloc().loc_for_frame(art, frame, instant=instant)

    def resolve_value(self, art, frame, path, instant=None, depth=5):
        return self._rrloc().resolve_value(art, frame, path,
                                           instant=instant, depth=depth)

    def warm(self, tid):
        return self._rrloc().warm_trace(tid)

    def precompute(self, art, log=print):
        return self._rrloc().precompute(art, log=log)


class PyMonitoring:
    """sys.monitoring eager capture. Tier-3 is an INDEX into recorded state;
    nothing is retrievable after the target process exits, and the
    capabilities + refusals say so."""
    id = "py-monitoring"
    language = "python"
    CAPS = {"reverse_step": True, "loc_state": True, "value_tree": True,
            "spans": False, "counterfactual": False}

    def capabilities(self):
        return dict(self.CAPS)

    def record(self, log=print, **params):
        import pipeline
        return pipeline.record_python(params["script"], params["src"], log=log)

    def extract(self, tid, log=print):
        # capture already wrote the frame stream; just assert it exists
        fp = os.path.join(layout.recording_dir(tid), "frames_py.jsonl")
        if not os.path.exists(fp):
            raise LocUnavailable(f"no python recording for `{tid}`")

    def artifact_env(self, tid):
        import json
        return {"CAIRN_OUT": os.path.join(layout.recording_dir(tid),
                                          "frames_py.jsonl"),
                "CAIRN_CRATE": "python", "CAIRN_SCENARIO_ID": tid,
                "CAIRN_LANGUAGE": self.language,
                "CAIRN_CAPTURE_BACKEND": self.id,
                "CAIRN_CAPABILITIES": json.dumps(self.CAPS),
                "CAIRN_ARTIFACT": layout.artifact_path(tid)}

    def loc(self, art, frame, instant=None):
        import pyloc
        return pyloc.loc_for_frame(art, frame, instant, err=LocUnavailable)

    def resolve_value(self, art, frame, path, instant=None, depth=5):
        raise LocUnavailable(
            "value expansion is not supported by the py-monitoring backend: "
            "state was recorded eagerly at a fixed depth and the process has "
            "exited — clipped nodes are deref.status=unreadable, not unexpanded")

    def warm(self, tid):
        return tid        # nothing to pin; serving is a file index

    def precompute(self, art, log=print):
        return 0          # eager recording IS the precompute


REGISTRY = {b.id: b for b in (RustRr(), PyMonitoring())}


def for_artifact(art):
    """Backend for an artifact, by its DECLARED capture_backend."""
    bid = art["scenario"].get("capture_backend", "")
    if bid in REGISTRY:
        return REGISTRY[bid]
    if bid.endswith("-dwarf"):          # gdb-dwarf dev path uses the rr code
        return REGISTRY["rr-dwarf"]
    raise LocUnavailable(f"no backend registered for `{bid}`")


def detect(tid):
    """Backend for a trace, by what its recording IS (used by derive before an
    artifact exists). Never guesses: unknown recording shape is an error."""
    if layout.rr_trace(tid):
        return REGISTRY["rr-dwarf"]
    if os.path.exists(os.path.join(layout.recording_dir(tid), "frames_py.jsonl")):
        return REGISTRY["py-monitoring"]
    raise LocUnavailable(f"no recording for `{tid}` (run `cairn record`)")
