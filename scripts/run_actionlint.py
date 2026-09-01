"""Validate every shipped GitHub Actions workflow with pinned actionlint."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from yaga.files import read_file_prefix

ROOT = Path(__file__).resolve().parents[1]
ACTIONLINT_IMAGE = (
    "rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
)
MAX_WORKFLOW_BYTES = 1024 * 1024


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
    for relative_path in workflow_paths():
        path = ROOT / relative_path
        try:
            workflow = read_file_prefix(path, maximum=MAX_WORKFLOW_BYTES)
        except OSError as error:
            raise SystemExit(f"could not read workflow {relative_path}: {error}") from error
        if len(workflow) > MAX_WORKFLOW_BYTES:
            raise SystemExit(f"workflow exceeds {MAX_WORKFLOW_BYTES} bytes: {relative_path}")
        command = [
            docker,
            "run",
            "--rm",
            "-i",
            ACTIONLINT_IMAGE,
            "-color",
            "-stdin-filename",
            relative_path,
            "-",
        ]
        returncode = subprocess.run(command, input=workflow, check=False).returncode
        if returncode:
            return returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
