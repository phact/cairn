"""Shared engine errors (core — importable by any backend without cycles)."""


class LocUnavailable(Exception):
    """Tier-3 state cannot be served for this request: missing recording,
    unsupported capability, or (py-monitoring) state not eagerly recorded."""
    pass
