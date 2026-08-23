"""Validate every shipped GitHub Actions workflow with pinned actionlint."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIONLINT_IMAGE = (
    "rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
)


def workflow_paths() -> list[str]:
    """Return deterministic container-relative workflow paths."""
    paths = set((ROOT / ".github" / "workflows").glob("*.yml"))
    paths.update((ROOT / ".github" / "workflows").glob("*.yaml"))
    paths.update((ROOT / "examples").glob("*.yml"))
    paths.update((ROOT / "examples").glob("*.yaml"))
    if not paths:
        raise SystemExit("no GitHub Actions workflows were found")
    return [path.relative_to(ROOT).as_posix() for path in sorted(paths)]


def main() -> int:
    """Run the immutable official actionlint image against all workflows."""
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker is required for the pinned actionlint gate")
    command = [
        docker,
        "run",
        "--rm",
        "--volume",
        f"{ROOT}:/repo:ro",
        "--workdir",
        "/repo",
        ACTIONLINT_IMAGE,
        "-color",
    ]
    command.extend(workflow_paths())
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
