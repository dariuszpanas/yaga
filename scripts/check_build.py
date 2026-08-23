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
    """Require one wheel and sdist containing the public package."""
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
        with zipfile.ZipFile(wheels[0]) as wheel:
            if "yaga/__init__.py" not in wheel.namelist():
                raise SystemExit("wheel does not contain the yaga package")
        with tarfile.open(source_distributions[0], mode="r:gz") as source_distribution:
            if not any(
                name.endswith("/src/yaga/__init__.py") for name in source_distribution.getnames()
            ):
                raise SystemExit("source distribution does not contain the yaga package")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
