"""Command-line boundary for the YAGA composite action."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from yaga.action import gate_name
from yaga.codex.runtime import operation_name, run_action
from yaga.errors import GateError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", required=True)
    parser.add_argument("--operation", required=True)
    return parser


def _runtime(gate: str) -> Callable[[], int]:
    """Select the shipped gate runtime."""
    if gate == "codex-review":
        return run_action
    raise GateError("gate is invalid")


def main(argv: list[str] | None = None) -> int:
    """Validate public inputs and delegate to one gate implementation."""
    args = _parser().parse_args(argv)
    try:
        gate = gate_name(args.gate)
        operation_name(args.operation)
        return _runtime(gate)()
    except GateError as error:
        print(f"YAGA failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
