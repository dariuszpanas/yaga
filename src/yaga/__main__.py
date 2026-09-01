"""Select the installed CLI or dependency-free composite Action boundary."""

from __future__ import annotations

import os

if os.environ.get("YAGA_ACTION_RUNTIME") == "1":
    from yaga.action_cli import main

    raise SystemExit(main())

from yaga.cli import app

app(prog_name="yaga")
