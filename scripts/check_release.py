"""Validate a production tag against package metadata and dated release notes."""

from __future__ import annotations

import argparse
import datetime
import re
import tomllib
from pathlib import Path


def validate_release(tag: str, version: str, changelog: str) -> None:
    """Reject mismatched versions and undated or ambiguous release headings."""
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise ValueError("release tag must have the form vMAJOR.MINOR.PATCH")
    if tag != f"v{version}":
        raise ValueError("release tag does not match the package version")
    dates = re.findall(
        rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$", changelog, re.M
    )
    if len(dates) != 1:
        raise ValueError("release requires exactly one dated changelog heading")
    datetime.date.fromisoformat(dates[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    validate_release(args.tag, project["version"], (root / "docs/changelog.md").read_text("utf-8"))


if __name__ == "__main__":
    main()
