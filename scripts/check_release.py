"""Validate a production tag against package metadata, Cargo workspace version, and release notes."""

from __future__ import annotations

import argparse
import datetime
import re
import tomllib
from pathlib import Path


def validate_release(
    tag: str,
    version: str,
    changelog: str,
    *,
    runtime_version: str | None = None,
    core_dependency_version: str | None = None,
    action_versions: set[str] | None = None,
) -> None:
    """Reject mismatched versions and undated or ambiguous release headings."""
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise ValueError("release tag must have the form vMAJOR.MINOR.PATCH")
    if tag != f"v{version}":
        raise ValueError("release tag does not match the package version")
    if runtime_version is not None and runtime_version != version:
        raise ValueError("runtime Cargo workspace version does not match the package version")
    if core_dependency_version is not None and core_dependency_version != version:
        raise ValueError(
            "Cargo workspace yaga-core dependency version does not match the package version"
        )
    if action_versions is not None and action_versions != {version}:
        raise ValueError("action.yml version references do not match the package version")
    dates = re.findall(
        rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$", changelog, re.M
    )
    if len(dates) != 1:
        raise ValueError("release requires exactly one dated changelog heading")
    datetime.date.fromisoformat(dates[0])


def _read_cargo_versions(root: Path) -> tuple[str, str]:
    cargo = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    workspace = cargo.get("workspace", {})
    version = workspace.get("package", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("Cargo.toml must define [workspace.package].version")
    core_dep = workspace.get("dependencies", {}).get("yaga-core")
    core_version = core_dep.get("version") if isinstance(core_dep, dict) else None
    if not isinstance(core_version, str) or not core_version:
        raise ValueError("Cargo.toml must define [workspace.dependencies].yaga-core.version")
    return version, core_version


def _read_action_versions(root: Path) -> set[str]:
    text = (root / "action.yml").read_text(encoding="utf-8")
    if "Â·" in text:
        raise ValueError("action.yml contains mojibake separator bytes")
    found = re.findall(r"(?:version:\s+|\*\*Version:\*\*\s+`)(\d+\.\d+\.\d+)", text)
    if len(found) < 5:
        raise ValueError("action.yml must report its version in banner and summary fallbacks")
    return set(found)


def verify_bundled_assets(root: Path) -> None:
    """Verify crate-bundled installers and license files match canonical repository copies."""
    pairs = (
        ("docs/install.sh", "crates/yaga-cli/assets/install.sh"),
        ("docs/install.ps1", "crates/yaga-cli/assets/install.ps1"),
        ("LICENSE", "crates/yaga-core/LICENSE"),
        ("LICENSE", "crates/yaga-cli/LICENSE"),
    )
    for canonical, bundled in pairs:
        source = root / canonical
        copy = root / bundled
        if not source.is_file() or not copy.is_file():
            raise ValueError(f"missing required release asset pair: {canonical} and {bundled}")
        if source.read_bytes() != copy.read_bytes():
            raise ValueError(f"{bundled} does not match {canonical}")


def release_notes(version: str, changelog: str) -> str:
    """Extract only the validated release section for the GitHub announcement."""
    validate_release(f"v{version}", version, changelog)
    heading = re.search(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.M
    )
    assert heading is not None
    section = re.split(r"^## ", changelog[heading.end() :], maxsplit=1, flags=re.M)[0].strip()
    if not section:
        raise ValueError("release notes must describe the selected version")
    return (
        section
        + f"\n\n[PyPI](https://pypi.org/project/yaga-cli/{version}/) · "
        + f"[crates.io (`yaga-cli`)](https://crates.io/crates/yaga-cli/{version}) · "
        + f"[crates.io (`yaga-core`)](https://crates.io/crates/yaga-core/{version})\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", help="default: the package metadata version")
    parser.add_argument("--notes-file", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    changelog = (root / "docs/changelog.md").read_text("utf-8")
    runtime_version, core_dependency_version = _read_cargo_versions(root)
    validate_release(
        args.tag or f"v{project['version']}",
        project["version"],
        changelog,
        runtime_version=runtime_version,
        core_dependency_version=core_dependency_version,
        action_versions=_read_action_versions(root),
    )
    verify_bundled_assets(root)
    if args.notes_file is not None:
        args.notes_file.write_text(release_notes(project["version"], changelog), encoding="utf-8")


if __name__ == "__main__":
    main()
