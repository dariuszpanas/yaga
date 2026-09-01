"""Select the installed CLI or dependency-free composite Action boundary."""

from __future__ import annotations

import os
import sys

if (
    os.environ.get("YAGA_ACTION_RUNTIME") == "1"
    and os.environ.get("YAGA_COMMIT_ACTION_RUNTIME") == "1"
):
    print("YAGA failed: multiple Action runtimes were selected", file=sys.stderr)
    raise SystemExit(2)

if os.environ.get("YAGA_ACTION_RUNTIME") == "1":
    from yaga.action_cli import main

    raise SystemExit(main())

if os.environ.get("YAGA_COMMIT_ACTION_RUNTIME") == "1":
    from yaga.commit_action_cli import main

    raise SystemExit(main())

from yaga.cli import app

app(prog_name="yaga")
