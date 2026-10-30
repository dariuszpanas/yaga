"""Minimum dependency security must fail closed on findings or incomplete audit evidence."""

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import check_dependency_audit as audit
from yaga.git.runtime import ProcessResult


def distributions() -> dict[str, str]:
    return {
        "yaga-cli": "0.4.1",
        "Pygments": "2.20.0",
        "PyYAML": "6.0.3",
        "rich": "13.8.0",
        "typer": "0.27.2",
    }


def pins() -> dict[str, str]:
    return audit.dependency_pins(distributions(), expected_yaga_version="0.4.1")


def response() -> dict:
    return {
        "dependencies": [
            {"name": name, "version": version, "vulns": []} for name, version in pins().items()
        ],
        "fixes": [],
    }


def process(stdout: bytes, *, returncode: int = 0, **flags: bool) -> ProcessResult:
    return ProcessResult(
        returncode,
        stdout,
        b"",
        flags.get("stdout_overflow", False),
        flags.get("stderr_overflow", False),
        flags.get("timed_out", False),
    )


def test_exact_installed_pins_exclude_only_the_verified_project() -> None:
    selected = distributions()
    selected["colorama"] = "0.3.5"
    actual = audit.dependency_pins(selected, expected_yaga_version="0.4.1")
    assert actual == {**pins(), "colorama": "0.3.5"}
    selected["yaga-cli"] = "0.4.0"
    with pytest.raises(ValueError, match="selected wheel"):
        audit.dependency_pins(selected, expected_yaga_version="0.4.1")


@pytest.mark.parametrize("name", ["PYGMENTS", "yaga_cli"])
def test_duplicate_normalized_installed_names_are_not_lost(name: str) -> None:
    selected = distributions()
    selected[name] = "0.4.1"
    with pytest.raises(ValueError, match="duplicate"):
        audit.dependency_pins(selected, expected_yaga_version="0.4.1")


def test_clean_response_covers_every_installed_pin() -> None:
    document = response()
    result = audit.validate_audit_response(json.dumps(document).encode(), pins(), returncode=0)
    assert result["status"] == "passed"
    assert result["audited_dependencies"] == len(pins())
    assert result["advisory_records"] == result["skipped_dependencies"] == 0
    assert result["response"] == document


def test_vulnerable_pygments_remains_a_failed_security_result() -> None:
    selected = pins() | {"pygments": "2.13.0"}
    document = response()
    dependency = next(item for item in document["dependencies"] if item["name"] == "pygments")
    dependency["version"] = "2.13.0"
    dependency["vulns"] = [
        {"id": "PYSEC-2023-117", "aliases": ["CVE-2022-40896"], "fix_versions": ["2.15.1"]},
        {"id": "PYSEC-2026-2987", "aliases": ["CVE-2026-4539"], "fix_versions": ["2.20.0"]},
    ]
    result = audit.validate_audit_response(json.dumps(document).encode(), selected, returncode=1)
    assert result["status"] == "findings"
    assert result["advisory_records"] == 2
    with pytest.raises(ValueError, match="exit code"):
        audit.validate_audit_response(json.dumps(document).encode(), selected, returncode=0)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "duplicate",
        "wrong-version",
        "skip",
        "null-vulns",
        "bad-id",
        "bad-aliases",
        "fixes",
    ],
)
def test_incomplete_or_ambiguous_responses_cannot_pass(mutation: str) -> None:
    document = response()
    dependencies = document["dependencies"]
    if mutation == "missing":
        dependencies.pop()
    elif mutation == "extra":
        dependencies.append({"name": "extra", "version": "1", "vulns": []})
    elif mutation == "duplicate":
        dependencies[-1] = {**dependencies[0], "name": dependencies[0]["name"].upper()}
    elif mutation == "wrong-version":
        dependencies[0]["version"] = "0.0.0"
    elif mutation == "skip":
        dependencies[0]["skip_reason"] = "package unavailable"
    elif mutation == "null-vulns":
        dependencies[0]["vulns"] = None
    elif mutation == "bad-id":
        dependencies[0]["vulns"] = [{"id": False}]
    elif mutation == "bad-aliases":
        dependencies[0]["vulns"] = [{"id": "CVE-2026-4539", "aliases": "not-a-list"}]
    else:
        document["fixes"] = [{"name": "pygments"}]
    with pytest.raises(ValueError):
        audit.validate_audit_response(json.dumps(document).encode(), pins(), returncode=0)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not-json",
        b"\xff",
        b"[]",
        b'{"dependencies":[],"dependencies":[],"fixes":[]}',
        b'{"dependencies":NaN,"fixes":[]}',
        b"{" * 2000,
    ],
)
def test_malformed_or_duplicate_json_fails(raw: bytes) -> None:
    with pytest.raises(ValueError):
        audit.validate_audit_response(raw, pins(), returncode=0)


@pytest.mark.parametrize("returncode", [1, 2, -1])
def test_clean_json_cannot_override_process_failure(returncode: int) -> None:
    with pytest.raises(ValueError, match="exit code"):
        audit.validate_audit_response(
            json.dumps(response()).encode(), pins(), returncode=returncode
        )


def test_auditor_uses_separate_locked_interpreter_and_private_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setenv("PIP_AUDIT_IGNORE_VULNS", "PYSEC-2026-2987")
    monkeypatch.setenv("PIP_AUDIT_VULNERABILITY_SERVICE", "osv")
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    monkeypatch.setenv("GH_TOKEN", "private-token")

    def run(command, **options):
        calls.append((command, options))
        if command[-1] == "--version":
            return process(b"pip-audit 2.10.1\n")
        requirements = Path(command[command.index("--requirement") + 1])
        assert requirements.read_text().splitlines() == [
            f"{name}=={version}" for name, version in pins().items()
        ]
        assert "yaga-cli" not in requirements.read_text()
        return process(json.dumps(response()).encode())

    monkeypatch.setattr(audit, "run_bounded_process", run)
    result = audit.audit_dependencies(
        distributions(), expected_yaga_version="0.4.1", workspace=tmp_path
    )
    command, options = calls[-1]
    assert command[:4] == [sys.executable, "-I", "-m", "pip_audit"]
    for flag in ("--strict", "--no-deps", "--disable-pip"):
        assert flag in command
    assert command[command.index("--vulnerability-service") + 1] == "pypi"
    assert command[command.index("--index-url") + 1] == "https://pypi.org/simple"
    assert command[command.index("--desc") + 1] == "off"
    environment = options["environment"]
    assert not any(key.startswith("PIP_AUDIT_") for key in environment)
    assert "PYTHONPATH" not in environment and "GH_TOKEN" not in environment
    assert not options["cwd"].exists()
    assert result["status"] == "passed"
    assert result["auditor_version"] == "2.10.1"
    assert result["dependencies"] == pins()
    assert result["response"] == response()


@pytest.mark.parametrize("failure", ["findings", "outage", "timeout", "overflow", "missing-tool"])
def test_audit_failures_retain_evidence_and_never_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def run(command, **_options):
        if command[-1] == "--version":
            return process(
                b"wrong-version\n" if failure == "missing-tool" else b"pip-audit 2.10.1\n"
            )
        if failure == "findings":
            document = response()
            document["dependencies"][0]["vulns"] = [{"id": "CVE-2026-4539"}]
            return process(json.dumps(document).encode(), returncode=1)
        if failure == "outage":
            return process(b"", returncode=2)
        return process(
            json.dumps(response()).encode(),
            timed_out=failure == "timeout",
            stdout_overflow=failure == "overflow",
        )

    monkeypatch.setattr(audit, "run_bounded_process", run)
    with pytest.raises(audit.DependencyAuditError) as caught:
        audit.audit_dependencies(distributions(), expected_yaga_version="0.4.1", workspace=tmp_path)
    evidence = caught.value.evidence
    assert evidence["status"] != "passed"
    assert evidence["dependencies"] == pins()
    assert "completed_at_utc" in evidence
    if failure != "missing-tool":
        assert "stdout" in evidence and "exit_code" in evidence


def test_audit_cleanup_failure_cannot_retain_a_passed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_directory = audit.tempfile.TemporaryDirectory

    @contextmanager
    def cleanup_failure(**options):
        with real_directory(**options) as directory:
            yield directory
        raise OSError("controlled cleanup failure")

    def run(command, **_options):
        return process(
            b"pip-audit 2.10.1\n" if command[-1] == "--version" else json.dumps(response()).encode()
        )

    monkeypatch.setattr(audit.tempfile, "TemporaryDirectory", cleanup_failure)
    monkeypatch.setattr(audit, "run_bounded_process", run)
    with pytest.raises(audit.DependencyAuditError) as caught:
        audit.audit_dependencies(distributions(), expected_yaga_version="0.4.1", workspace=tmp_path)
    assert caught.value.evidence["status"] == "error"
    assert caught.value.evidence["response"] == response()
    assert caught.value.evidence["error"] == "controlled cleanup failure"
