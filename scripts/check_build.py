"""Build and inspect YAGA distributions with the locked backend."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """Require one wheel and sdist containing both supported execution surfaces."""
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required for the package build gate")
    with tempfile.TemporaryDirectory(prefix="yaga-build-") as temporary_directory:
        output = Path(temporary_directory)
        subprocess.run(
            [
                uv,
                "build",
                "--no-build-isolation",
                "--out-dir",
                str(output),
            ],
            cwd=ROOT,
            check=True,
        )
        wheels = list(output.glob("*.whl"))
        source_distributions = list(output.glob("*.tar.gz"))
        if len(wheels) != 1 or len(source_distributions) != 1:
            raise SystemExit("build must emit exactly one wheel and one source distribution")
        if not wheels[0].name.startswith("yaga_cli-"):
            raise SystemExit("wheel has the wrong distribution name")
        with zipfile.ZipFile(wheels[0]) as wheel:
            names = set(wheel.namelist())
            required = {
                "yaga/__init__.py",
                "yaga/action_cli.py",
                "yaga/commit_action_cli.py",
                "yaga/commit_action_runtime.py",
                "yaga/cli.py",
                "yaga/codex/runtime.py",
                "yaga/commands/commit.py",
                "yaga/commands/github.py",
                "yaga/commits/checker.py",
                "yaga/commits/github_event.py",
                "yaga/commits/github_reporting.py",
                "yaga/files.py",
            }
            if missing := sorted(required - names):
                raise SystemExit(f"wheel is missing required modules: {', '.join(missing)}")
            entry_points = next(
                (name for name in names if name.endswith(".dist-info/entry_points.txt")),
                None,
            )
            if entry_points is None:
                raise SystemExit("wheel does not contain console-script metadata")
            entry_point_text = wheel.read(entry_points).decode("utf-8")
            if "yaga = yaga.cli:main" not in entry_point_text:
                raise SystemExit("wheel does not expose the yaga console script")
        with tarfile.open(source_distributions[0], mode="r:gz") as source_distribution:
            names = source_distribution.getnames()
            required_suffixes = {
                "/action.yml": "the composite Action",
                "/actions/commit-check/action.yml": "the commit-check Action",
                "/.github/workflows/commit-policy.yml": "the commit-policy dogfood workflow",
                "/.pre-commit-hooks.yaml": "the pre-commit provider manifest",
                "/examples/codex-review.yml": "the Codex review workflow example",
                "/examples/commit-policy.yml": "the commit-policy workflow example",
                "/examples/review-policy.yml": "the review policy workflow example",
                "/src/yaga/__init__.py": "the yaga package",
            }
            for suffix, label in required_suffixes.items():
                if not any(name.endswith(suffix) for name in names):
                    raise SystemExit(f"source distribution does not contain {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
