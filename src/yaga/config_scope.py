"""Installation-aware fallback configuration without third-party dependencies."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def global_config_path() -> Path | None:
    """Allow user defaults for system/tool installations, never project environments or CI."""
    if any(
        os.environ.get(key)
        for key in ("CI", "GITHUB_ACTIONS", "YAGA_ACTION_RUNTIME", "YAGA_COMMIT_ACTION_RUNTIME")
    ):
        return None
    prefix = Path(sys.prefix)
    if sys.prefix != sys.base_prefix and not any(
        (prefix / marker).is_file() for marker in ("uv-receipt.toml", "pipx_metadata.json")
    ):
        return None
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    if not base.is_absolute():
        return None
    return base / "yaga" / "config.toml"
