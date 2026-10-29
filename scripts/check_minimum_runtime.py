"""Qualify a fresh base wheel against lowest-resolved runtime dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from .check_build import exercise_installed_runtime, run_bounded, successful_json
else:
    from check_build import exercise_installed_runtime, run_bounded, successful_json

ROOT = Path(__file__).resolve().parents[1]
IMPORTED_MODULES = ("yaga", "yaga.cli", "yaml", "typer", "rich")
INVENTORY_SCRIPT = """\
import importlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

modules = ("yaga", "yaga.cli", "yaml", "typer", "rich")
print(json.dumps({
    "python": platform.python_version(),
    "platform": platform.platform(),
    "prefix": str(Path(sys.prefix).resolve()),
    "imports": {
        name: str(Path(importlib.import_module(name).__file__).resolve())
        for name in modules
    },
    "distributions": dict(sorted(
        (dist.metadata["Name"], dist.version)
        for dist in importlib.metadata.distributions()
    )),
}))
"""


def isolated_environment(source: dict[str, str]) -> dict[str, str]:
    """Exclude project, Python, and installer overrides from this independent resolution."""
    environment = {
        name: value
        for name, value in source.items()
        if not name.upper().startswith(("PYTHON", "UV_", "PIP_"))
        and name.upper() not in {"VIRTUAL_ENV", "YAGA_ACTION_RUNTIME", "YAGA_COMMIT_ACTION_RUNTIME"}
    }
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    return environment


def validate_inventory(inventory: dict[str, object], environment: Path) -> None:
    """Require all inspected imports and distribution metadata from the new environment."""
    expected = environment.resolve()
    if inventory.get("prefix") != str(expected):
        raise SystemExit("minimum runtime smoke used the wrong Python environment")
    imports = inventory.get("imports")
    if not isinstance(imports, dict) or set(imports) != set(IMPORTED_MODULES):
        raise SystemExit("minimum runtime inventory is missing expected imports")
    for location in imports.values():
        if not isinstance(location, str):
            raise SystemExit("minimum runtime inventory has an invalid import location")
        path = Path(location).resolve()
        if not path.is_relative_to(expected) or path.is_relative_to(ROOT):
            raise SystemExit(
                "minimum runtime smoke imported from outside its installed environment"
            )
    distributions = inventory.get("distributions")
    if not isinstance(distributions, dict) or not {"yaga-cli", "typer", "rich"} <= set(
        distributions
    ):
        raise SystemExit("minimum runtime inventory is missing installed distributions")


def qualify(uv: str, output: Path) -> dict[str, object]:
    """Build current sources, install only their base runtime, and run the shared smoke."""
    if output.resolve().is_relative_to(ROOT):
        raise SystemExit("minimum runtime temporary directory must be outside the checkout")
    environment = isolated_environment(dict(os.environ))

    def run(arguments: list[str], *, cwd: Path = output) -> None:
        subprocess.run(
            [uv, "--no-config", "--no-python-downloads", *arguments],
            cwd=cwd,
            env=environment,
            check=True,
            timeout=300,
        )

    run(
        [
            "build",
            "--wheel",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--out-dir",
            str(output),
            str(ROOT),
        ]
    )
    wheels = list(output.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit("minimum runtime build must produce exactly one wheel")
    wheel = wheels[0]
    runtime = output / "runtime"
    run(["venv", "--no-project", "--python", sys.executable, str(runtime)])
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run(
        [
            "pip",
            "install",
            "--python",
            str(python),
            "--resolution",
            "lowest",
            "--no-build",
            str(wheel),
        ]
    )
    run(["pip", "check", "--python", str(python)])
    inventory = successful_json(
        run_bounded([str(python), "-I", "-c", INVENTORY_SCRIPT], cwd=output, env=environment),
        operation="minimum runtime inventory",
    )
    validate_inventory(inventory, runtime)
    exercise_installed_runtime(runtime, output)
    return {
        "schema_version": 1,
        "status": "passed",
        "resolution": "lowest",
        "scope": "base-runtime",
        "wheel": {"name": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        "uv": subprocess.check_output([uv, "--version"], text=True).strip(),
        "runtime": inventory,
        "checks": ["dependency-consistency", "installed-imports", "shared-installed-wheel-smoke"],
    }


def main() -> int:
    """Write a receipt only after every lowest-runtime check passes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path, help="New JSON evidence file")
    args = parser.parse_args()
    report = args.report.resolve()
    if report.exists():
        raise SystemExit("minimum runtime report must not already exist")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required for minimum runtime qualification")
    with tempfile.TemporaryDirectory(prefix="yaga-minimum-runtime-") as directory:
        document = qualify(uv, Path(directory))
    with report.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(document, indent=2) + "\n")
    print(f"Minimum runtime qualification passed; evidence: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
