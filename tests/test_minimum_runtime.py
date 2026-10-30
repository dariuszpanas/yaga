"""Lowest-runtime evidence must exclude ambient environments and checkout imports."""

import json
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import check_minimum_runtime
from scripts.check_dependency_audit import DependencyAuditError
from yaga.git.runtime import ProcessResult


def test_installer_and_python_overrides_do_not_change_lowest_runtime() -> None:
    environment = check_minimum_runtime.isolated_environment(
        {
            "PATH": "tools",
            "SYSTEMROOT": "system",
            "VIRTUAL_ENV": "checkout/.venv",
            "PYTHONPATH": "checkout/src",
            "PYTHONHOME": "another-python",
            "UV_RESOLUTION": "highest",
            "UV_CONSTRAINT": "constraints.txt",
            "PIP_INDEX_URL": "https://other.invalid/simple",
            "YAGA_ACTION_RUNTIME": "1",
            "YAGA_COMMIT_ACTION_RUNTIME": "1",
        }
    )
    assert environment == {
        "PATH": "tools",
        "SYSTEMROOT": "system",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }


def inventory(environment: Path) -> dict[str, object]:
    return {
        "prefix": str(environment.resolve()),
        "imports": {
            name: str(environment / "site-packages" / name / "__init__.py")
            for name in check_minimum_runtime.IMPORTED_MODULES
        },
        "distributions": {"yaga-cli": "0.0.0", "typer": "0.27.2", "rich": "13.8.0"},
    }


def test_inventory_accepts_only_the_new_environment(tmp_path: Path) -> None:
    document = inventory(tmp_path / "runtime")
    check_minimum_runtime.validate_inventory(document, tmp_path / "runtime")
    with pytest.raises(SystemExit, match="wrong Python environment"):
        check_minimum_runtime.validate_inventory(document, tmp_path / "another-runtime")


@pytest.mark.parametrize("source", ["checkout", "other-environment"])
def test_inventory_rejects_leaking_imports(tmp_path: Path, source: str) -> None:
    document = inventory(tmp_path / "runtime")
    imports = document["imports"]
    assert isinstance(imports, dict)
    imports["yaga.cli"] = str(
        check_minimum_runtime.ROOT / "src/yaga/cli.py"
        if source == "checkout"
        else tmp_path / "other-environment/yaga/cli.py"
    )
    with pytest.raises(SystemExit, match="outside its installed environment"):
        check_minimum_runtime.validate_inventory(document, tmp_path / "runtime")


def test_inventory_requires_cli_and_dependency_provenance(tmp_path: Path) -> None:
    document = inventory(tmp_path / "runtime")
    imports = document["imports"]
    assert isinstance(imports, dict)
    del imports["typer"]
    with pytest.raises(SystemExit, match="missing expected imports"):
        check_minimum_runtime.validate_inventory(document, tmp_path / "runtime")


def make_wheel(path: Path, *, metadata: str | None = None) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "yaga_cli-0.4.1.dist-info/METADATA",
            metadata or "Metadata-Version: 2.4\nName: yaga-cli\nVersion: 0.4.1\n\n",
        )


@pytest.mark.parametrize(
    "metadata",
    [
        "Name: other-package\nVersion: 0.4.1\n\n",
        "Name: yaga-cli\nName: other\nVersion: 0.4.1\n\n",
        "Name: yaga-cli\nVersion: 0.4.1\nVersion: 0.4.0\n\n",
        "Name: yaga-cli\n\n",
    ],
)
def test_selected_wheel_requires_one_project_identity(tmp_path: Path, metadata: str) -> None:
    wheel = tmp_path / "yaga_cli-0.4.1-py3-none-any.whl"
    make_wheel(wheel, metadata=metadata)
    with pytest.raises(ValueError):
        check_minimum_runtime.inspect_wheel(wheel)


@pytest.mark.parametrize("failure", [None, "wrong-version", "advisories", "changed-wheel", "smoke"])
def test_retained_wheel_is_not_rebuilt_and_requires_every_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    wheel = tmp_path / "yaga_cli-0.4.1-py3-none-any.whl"
    make_wheel(wheel)
    original_bytes = wheel.read_bytes()
    output = tmp_path / "qualification"
    output.mkdir()
    commands = []

    def run(command, **_options):
        commands.append(command)
        assert "build" not in command
        return ProcessResult(
            0, b"uv 0.13.5\n" if command[-1] == "--version" else b"", b"", False, False, False
        )

    def inspect(command, **_options):
        document = inventory(output / "runtime")
        document["distributions"] = {
            "yaga-cli": "0.4.0" if failure == "wrong-version" else "0.4.1",
            "typer": "0.27.2",
            "rich": "13.8.0",
            "Pygments": "2.20.0",
            "PyYAML": "6.0.3",
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(document).encode(), b"")

    def smoke(_runtime, _output):
        if failure == "smoke":
            raise SystemExit("installed reporting smoke failed")

    def audit(distributions, **options):
        assert options["expected_yaga_version"] == "0.4.1"
        assert distributions["Pygments"] == "2.20.0"
        if failure == "advisories":
            raise DependencyAuditError(
                "known advisories", {"status": "findings", "advisory_records": 1}
            )
        if failure == "changed-wheel":
            make_wheel(wheel, metadata="Name: yaga-cli\nVersion: 0.4.0\n\n")
        return {"status": "passed", "advisory_records": 0, "skipped_dependencies": 0}

    monkeypatch.setattr(check_minimum_runtime, "run_bounded_process", run)
    monkeypatch.setattr(check_minimum_runtime, "run_bounded", inspect)
    monkeypatch.setattr(check_minimum_runtime, "exercise_installed_runtime", smoke)
    monkeypatch.setattr(check_minimum_runtime, "audit_dependencies", audit)
    if failure:
        with pytest.raises(check_minimum_runtime.MinimumRuntimeError) as caught:
            check_minimum_runtime.qualify("uv", output, wheel=wheel)
        assert caught.value.evidence["status"] != "passed"
        assert "failure_phase" in caught.value.evidence
        if failure == "advisories":
            security = caught.value.evidence["dependency_audit"]
            assert isinstance(security, dict) and security["advisory_records"] == 1
    else:
        evidence = check_minimum_runtime.qualify("uv", output, wheel=wheel)
        assert evidence["status"] == "passed"
        identity, checks, security = (
            evidence["wheel"],
            evidence["checks"],
            evidence["dependency_audit"],
        )
        assert isinstance(identity, dict) and identity["source"] == "provided"
        assert identity["version"] == "0.4.1"
        assert isinstance(checks, list) and "dependency-audit" in checks
        assert isinstance(security, dict) and security["status"] == "passed"
        assert wheel.read_bytes() == original_bytes
    assert (output / "selected-wheel" / wheel.name).read_bytes() == original_bytes
    install = next(command for command in commands if "install" in command)
    assert "--no-cache" in install and "--no-build" in install
    assert install[install.index("--resolution") + 1] == "lowest"
    assert install[install.index("--default-index") + 1] == "https://pypi.org/simple"


def test_main_retains_a_failed_report_and_creates_its_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "evidence" / "minimum.json"
    monkeypatch.setattr(sys, "argv", ["check_minimum_runtime", "--report", str(report)])
    monkeypatch.setattr(check_minimum_runtime.shutil, "which", lambda _name: "uv")

    def fail(*_args, **_kwargs):
        raise check_minimum_runtime.MinimumRuntimeError(
            "security gate failed", {"status": "failed", "failure_phase": "dependency-audit"}
        )

    monkeypatch.setattr(check_minimum_runtime, "qualify", fail)
    assert check_minimum_runtime.main() == 1
    assert json.loads(report.read_text())["status"] == "failed"


def test_workspace_cleanup_failure_preserves_evidence_without_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "minimum.json"
    monkeypatch.setattr(sys, "argv", ["check_minimum_runtime", "--report", str(report)])
    monkeypatch.setattr(check_minimum_runtime.shutil, "which", lambda _name: "uv")
    real_directory = check_minimum_runtime.tempfile.TemporaryDirectory

    @contextmanager
    def cleanup_failure(**options):
        with real_directory(**options) as directory:
            yield directory
        raise OSError("controlled workspace cleanup failure")

    monkeypatch.setattr(check_minimum_runtime.tempfile, "TemporaryDirectory", cleanup_failure)
    monkeypatch.setattr(
        check_minimum_runtime,
        "qualify",
        lambda *_args, **_kwargs: {"status": "passed", "wheel": {"sha256": "verified"}},
    )
    assert check_minimum_runtime.main() == 1
    document = json.loads(report.read_text())
    assert document["status"] == "error"
    assert document["failure_phase"] == "workspace-cleanup"
    assert document["wheel"] == {"sha256": "verified"}
