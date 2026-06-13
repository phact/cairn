"""
Deterministic decode SPECS (core) — the single source of truth for the
always-on, additive decoders every backend implements.

The decode *implementations* are necessarily per-backend (each reads its own
runtime's object model: gdb.Value vs live Python objects), but the SPECS —
regexes, ranges, name lists, the arm vocabulary — must not drift between
copies. Before this module they were triplicated across artifact.py,
backends/rust/loc_server.py and backends/python/capture.py.

Everything here is pure and dependency-free so it imports cleanly inside gdb's
embedded Python and inside a monitored target process.
"""

import base64
import datetime
import json
import re

# ---- outcome arm vocabulary (shared across languages) -----------------------
ARMS = ("ok", "err", "some", "none", "true", "false")

OTHER_ARM = {
    "ok": ("err", "would have returned the error path"),
    "err": ("ok", "would have continued on the success value"),
    "some": ("none", "would have taken the missing-value path"),
    "none": ("some", "would have used the found value"),
    "true": ("false", "would have taken the false branch"),
    "false": ("true", "would have taken the true branch"),
}

SURPRISING_ARMS = ("err", "none", "false")

# ---- JWT → claims ------------------------------------------------------------
JWT_RE = re.compile(r"(eyJ[A-Za-z0-9_-]{4,})\.([A-Za-z0-9_-]{4,})\.([A-Za-z0-9_-]{2,})")


def _b64url(seg):
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def jwt_decode(text):
    """Header+claims of a JWT found in `text` (deterministic; signature NOT
    verified — we read recorded state, we don't assert validity)."""
    m = JWT_RE.search(text or "")
    if not m:
        return None
    try:
        return {"header": json.loads(_b64url(m.group(1))),
                "claims": json.loads(_b64url(m.group(2)))}
    except Exception:
        return None


# ---- Unix epoch → ISO (NAME-gated: range alone would mislabel counts/ids) ----
TIME_EXACT = {"iat", "exp", "nbf", "now", "timestamp", "time", "ts", "deadline",
              "expiry", "expires", "mtime", "ctime", "atime", "epoch"}
TIME_SUFFIX = ("_at", "_ts", "_time", "_secs", "_seconds", "_millis", "_ms",
               "_epoch", "_date")
TIME_SUBSTR = ("timestamp", "deadline", "expires", "expiry")
FN_TIME = ("now", "unix", "timestamp", "epoch")


def timeish_name(name):
    n = (name or "").lower()
    return (n in TIME_EXACT or n.endswith(TIME_SUFFIX)
            or any(x in n for x in TIME_SUBSTR))


def timeish_fn(fn_name):
    f = (fn_name or "").lower()
    return any(x in f for x in FN_TIME) or f.endswith(("_secs", "_at"))


def epoch_iso(n):
    """ISO reading for a plausible epoch in seconds or millis, else None."""
    secs = n if 1_000_000_000 <= n <= 4_102_444_800 else (
        n // 1000 if 1_000_000_000_000 <= n <= 4_102_444_800_000 else None)
    if secs is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(secs, datetime.timezone.utc) \
            .strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        return None
