"""Qualify a base wheel against functional and security checks of lowest dependencies."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import Version

from yaga.errors import safe_error_text
from yaga.git.runtime import run_bounded_process

if __package__:
    from .check_build import exercise_installed_runtime, run_bounded, successful_json
    from .check_dependency_audit import DependencyAuditError, audit_dependencies, dependency_pins
else:
    from check_build import exercise_installed_runtime, run_bounded, successful_json
    from check_dependency_audit import DependencyAuditError, audit_dependencies, dependency_pins

ROOT = Path(__file__).resolve().parents[1]
IMPORTED_MODULES = ("yaga", "yaga.cli", "yaml", "typer", "rich", "pygments")
MAX_WHEEL_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
INVENTORY_SCRIPT = """\
import importlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

modules = ("yaga", "yaga.cli", "yaml", "typer", "rich", "pygments")
distributions = {}
for dist in importlib.metadata.distributions():
    name = dist.metadata["Name"]
    if name in distributions:
        raise SystemExit("duplicate installed distribution name")
    distributions[name] = dist.version
print(json.dumps({
    "python": platform.python_version(),
    "platform": platform.platform(),
    "prefix": str(Path(sys.prefix).resolve()),
    "imports": {
        name: str(Path(importlib.import_module(name).__file__).resolve())
        for name in modules
    },
    "distributions": dict(sorted(distributions.items())),
}))
"""


class MinimumRuntimeError(RuntimeError):
    """Qualification failure with partial evidence suitable for a failed CI artifact."""

    def __init__(self, message: str, evidence: dict[str, object]) -> None:
        super().__init__(message)
        self.evidence = evidence


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


def inspect_wheel(path: Path) -> tuple[bytes, str]:
    """Read bounded wheel bytes and require exactly one YAGA metadata identity."""
    if not path.is_file() or path.suffix != ".whl":
        raise ValueError("selected wheel must be a regular .whl file")
    with path.open("rb") as stream:
        content = stream.read(MAX_WHEEL_BYTES + 1)
    if len(content) > MAX_WHEEL_BYTES:
        raise ValueError("selected wheel exceeds the qualification size limit")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = archive.infolist()
        metadata = [member for member in members if member.filename.endswith(".dist-info/METADATA")]
        if len(members) > 512 or len(metadata) != 1:
            raise ValueError("selected wheel has an ambiguous metadata inventory")
        with archive.open(metadata[0]) as stream:
            raw = stream.read(MAX_METADATA_BYTES + 1)
        if len(raw) > MAX_METADATA_BYTES:
            raise ValueError("selected wheel metadata exceeds its size limit")
    parsed = BytesParser().parsebytes(raw)
    names, versions = parsed.get_all("Name", []), parsed.get_all("Version", [])
    if len(names) != 1 or canonicalize_name(names[0], validate=True) != "yaga-cli":
        raise ValueError("selected wheel is not unambiguous yaga-cli metadata")
    if len(versions) != 1:
        raise ValueError("selected wheel has no unambiguous version")
    return content, str(Version(versions[0]))


def qualify(uv: str, output: Path, *, wheel: Path | None = None) -> dict[str, object]:
    """Build or select one wheel, then require functional and advisory qualification."""
    if output.resolve().is_relative_to(ROOT):
        raise SystemExit("minimum runtime temporary directory must be outside the checkout")
    environment = isolated_environment(dict(os.environ))
    checks: list[str] = []
    document: dict[str, object] = {
        "schema_version": 1,
        "status": "error",
        "resolution": "lowest",
        "scope": "base-runtime",
        "checks": checks,
    }
    operation = "select-wheel"

    def run(arguments: list[str], *, cwd: Path = output) -> bytes:
        result = run_bounded_process(
            [uv, "--no-config", "--no-cache", "--no-python-downloads", *arguments],
            cwd=cwd,
            environment=environment,
            stdout_limit=4 * 1024 * 1024,
            stderr_limit=64 * 1024,
            timeout_seconds=300,
        )
        if (
            result.returncode
            or result.timed_out
            or result.stdout_overflow
            or result.stderr_overflow
        ):
            raise ValueError(f"minimum runtime {operation} command failed")
        return result.stdout

    try:
        if wheel is None:
            operation = "build-wheel"
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
                raise ValueError("minimum runtime build must produce exactly one wheel")
            source = wheels[0]
        else:
            source = wheel.resolve(strict=True)
        operation = "inspect-wheel"
        content, version = inspect_wheel(source)
        digest = hashlib.sha256(content).hexdigest()
        document["wheel"] = {
            "name": source.name,
            "version": version,
            "sha256": digest,
            "source": "provided" if wheel is not None else "built",
        }
        private_wheel_directory = output / "selected-wheel"
        private_wheel_directory.mkdir()
        selected_wheel = private_wheel_directory / source.name
        selected_wheel.write_bytes(content)
        operation = "create-environment"
        runtime = output / "runtime"
        run(["venv", "--no-project", "--python", sys.executable, str(runtime)])
        python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        operation = "install-lowest"
        run(
            [
                "pip",
                "install",
                "--python",
                str(python),
                "--resolution",
                "lowest",
                "--no-build",
                "--default-index",
                "https://pypi.org/simple",
                str(selected_wheel),
            ]
        )
        operation = "dependency-consistency"
        run(["pip", "check", "--python", str(python)])
        checks.append(operation)
        operation = "installed-imports"
        inventory = successful_json(
            run_bounded([str(python), "-I", "-c", INVENTORY_SCRIPT], cwd=output, env=environment),
            operation="minimum runtime inventory",
        )
        document["runtime"] = inventory
        validate_inventory(inventory, runtime)
        dependency_pins(inventory["distributions"], expected_yaga_version=version)
        checks.append(operation)
        operation = "shared-installed-wheel-smoke"
        exercise_installed_runtime(runtime, output)
        checks.append(operation)
        operation = "dependency-audit"
        document["dependency_audit"] = audit_dependencies(
            inventory["distributions"], expected_yaga_version=version, workspace=output
        )
        checks.append(operation)
        operation = "artifact-identity"
        final_content, _ = inspect_wheel(source)
        if hashlib.sha256(final_content).hexdigest() != digest:
            raise ValueError("selected wheel changed during qualification")
        checks.append(operation)
        document["uv"] = run(["--version"]).decode("utf-8").strip()
        document["status"] = "passed"
        return document
    except DependencyAuditError as error:
        document["dependency_audit"] = error.evidence
        document["status"] = "failed" if error.evidence["status"] == "findings" else "error"
        document["failure_phase"] = operation
        raise MinimumRuntimeError(str(error), document) from error
    except (OSError, ValueError, SystemExit, zipfile.BadZipFile) as error:
        document["failure_phase"] = operation
        document["error"] = safe_error_text(error, maximum=300)
        raise MinimumRuntimeError(f"minimum runtime {operation} failed", document) from error


def main() -> int:
    """Retain success or failure evidence; all functional and audit checks are mandatory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path, help="New JSON evidence file")
    parser.add_argument("--wheel", type=Path, help="Qualify this retained wheel without rebuilding")
    args = parser.parse_args()
    report = args.report.resolve()
    if report.exists():
        raise SystemExit("minimum runtime report must not already exist")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required for minimum runtime qualification")
    report.parent.mkdir(parents=True, exist_ok=True)
    failure = None
    document: dict[str, object] = {"schema_version": 1, "status": "error"}
    workspace_started = False
    try:
        with tempfile.TemporaryDirectory(prefix="yaga-minimum-runtime-") as directory:
            workspace_started = True
            try:
                document = qualify(uv, Path(directory), wheel=args.wheel)
            except MinimumRuntimeError as error:
                document = error.evidence
                failure = str(error)
    except OSError:
        document["status"] = "error"
        document["failure_phase"] = "workspace-cleanup" if workspace_started else "workspace-setup"
        failure = "could not create or clean up the private qualification workspace"
    with report.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(document, indent=2) + "\n")
    if failure:
        print(f"Minimum runtime qualification failed: {failure}", file=sys.stderr)
        return 1
    print(f"Minimum runtime qualification passed; evidence: {safe_error_text(report)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
