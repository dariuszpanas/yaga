"""Dependency-free command boundary used by the composite Action."""

from __future__ import annotations

import sys

from yaga.action import run_gate
from yaga.errors import GateError, safe_error_text


def main(argv: list[str] | None = None) -> int:
    """Accept exactly one closed Action command and dispatch it fail-closed."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3 or arguments[0] != "gate":
        print("YAGA failed: action command is invalid", file=sys.stderr)
        return 1
    try:
        return run_gate(arguments[1], arguments[2])
    except GateError as error:
        print(f"YAGA failed: {safe_error_text(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
