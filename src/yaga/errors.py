"""Errors intentionally safe to expose in GitHub Actions logs."""


class GateError(RuntimeError):
    """Raised when evidence is missing, stale, malformed, or unsafe."""
