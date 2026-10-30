"""Audit an exact installed base-runtime inventory with the locked development tool."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import Version

from yaga.git.runtime import ProcessResult, run_bounded_process

AUDITOR_VERSION = "2.10.1"
MAX_AUDIT_BYTES = 4 * 1024 * 1024
MAX_AUDIT_ERROR_BYTES = 64 * 1024
MAX_DEPENDENCIES = 128


class DependencyAuditError(RuntimeError):
    """A failed audit and the bounded evidence collected before failure."""

    def __init__(self, message: str, evidence: dict[str, object]) -> None:
        super().__init__(message)
        self.evidence = evidence


def dependency_pins(distributions: object, *, expected_yaga_version: str) -> dict[str, str]:
    """Exclude only the version-verified YAGA distribution from the actual inventory."""
    if not isinstance(distributions, dict) or not 2 <= len(distributions) <= MAX_DEPENDENCIES:
        raise ValueError("installed dependency inventory is missing or over budget")
    pins: dict[str, str] = {}
    for name, version in distributions.items():
        normalized_name, normalized_version = normalize_pin(name, version)
        if normalized_name in pins:
            raise ValueError("installed inventory contains duplicate normalized names")
        pins[normalized_name] = normalized_version
    if pins.pop("yaga-cli", None) != str(Version(expected_yaga_version)):
        raise ValueError("installed YAGA version does not match the selected wheel")
    if not {"pygments", "pyyaml", "rich", "typer"} <= pins.keys():
        raise ValueError("installed base dependency inventory is incomplete")
    return dict(sorted(pins.items()))


def normalize_pin(name: object, version: object) -> tuple[str, str]:
    """Accept only bounded package names and exact, parseable installed versions."""
    if not isinstance(name, str) or not 1 <= len(name) <= 128:
        raise ValueError("dependency name is invalid")
    if not isinstance(version, str) or not 1 <= len(version) <= 128:
        raise ValueError("dependency version is invalid")
    try:
        return str(canonicalize_name(name, validate=True)), str(Version(version))
    except ValueError as error:
        raise ValueError("dependency name or exact version is invalid") from error


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("audit JSON contains a duplicate key")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> object:
    raise ValueError("audit JSON contains a nonfinite number")


def _valid_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and not any(ord(character) < 32 for character in value)
    )


def validate_audit_response(
    raw: bytes, pins: dict[str, str], *, returncode: int
) -> dict[str, object]:
    """Require a complete exact inventory; advisory output can never mean success."""
    if not raw or len(raw) > MAX_AUDIT_BYTES:
        raise ValueError("audit JSON is empty or over budget")
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("audit JSON is malformed") from error
    if not isinstance(document, dict) or set(document) != {"dependencies", "fixes"}:
        raise ValueError("audit JSON has an unexpected document shape")
    dependencies = document["dependencies"]
    if not isinstance(dependencies, list) or len(dependencies) != len(pins):
        raise ValueError("audit response omitted or added dependency records")
    if document["fixes"] != []:
        raise ValueError("read-only audit returned unexpected fixes")
    returned: dict[str, str] = {}
    advisory_ids: set[str] = set()
    finding_count = 0
    for dependency in dependencies:
        if not isinstance(dependency, dict) or "skip_reason" in dependency:
            raise ValueError("audit response skipped a dependency")
        if set(dependency) != {"name", "version", "vulns"}:
            raise ValueError("audit dependency record is malformed")
        name, version = normalize_pin(dependency["name"], dependency["version"])
        if name in returned or pins.get(name) != version:
            raise ValueError("audit dependency identity does not match the installed inventory")
        returned[name] = version
        findings = dependency["vulns"]
        if not isinstance(findings, list):
            raise ValueError("audit dependency omitted its advisory list")
        for finding in findings:
            if not isinstance(finding, dict) or not _valid_text(finding.get("id")):
                raise ValueError("audit advisory record is malformed")
            for key in ("aliases", "fix_versions"):
                if key in finding and (
                    not isinstance(finding[key], list)
                    or not all(_valid_text(value) for value in finding[key])
                ):
                    raise ValueError("audit advisory metadata is malformed")
            if "description" in finding and not isinstance(finding["description"], str):
                raise ValueError("audit advisory description is malformed")
            advisory_ids.add(finding["id"])
        finding_count += len(findings)
    if returned != pins:
        raise ValueError("audit response does not cover every installed dependency")
    if returncode != (1 if finding_count else 0):
        raise ValueError("audit exit code and advisory results disagree")
    return {
        "status": "findings" if finding_count else "passed",
        "audited_dependencies": len(returned),
        "skipped_dependencies": 0,
        "advisory_records": finding_count,
        "advisory_ids": sorted(advisory_ids),
        "response": document,
    }


def audit_environment(directory: Path, source: dict[str, str]) -> dict[str, str]:
    """Use only OS launch requirements; never inherit audit, installer, or auth overrides."""
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "PATH"}
    environment = {key: value for key, value in source.items() if key.upper() in allowed}
    environment.update(
        {
            "HOME": str(directory),
            "USERPROFILE": str(directory),
            "APPDATA": str(directory),
            "LOCALAPPDATA": str(directory),
            "TEMP": str(directory),
            "TMP": str(directory),
            "TMPDIR": str(directory),
            "XDG_CACHE_HOME": str(directory),
            "XDG_CONFIG_HOME": str(directory),
            "PIP_CONFIG_FILE": os.devnull,
            "NETRC": os.devnull,
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "NO_COLOR": "1",
        }
    )
    return environment


def _process_failed(result: ProcessResult) -> bool:
    return result.timed_out or result.stdout_overflow or result.stderr_overflow


def audit_dependencies(
    distributions: object, *, expected_yaga_version: str, workspace: Path
) -> dict[str, object]:
    """Audit actual pins in a fresh private cache using the separate locked interpreter."""
    evidence: dict[str, object] = {
        "status": "error",
        "auditor": "pip-audit",
        "expected_auditor_version": AUDITOR_VERSION,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "service": "pypi",
        "cache": "fresh private directory",
    }
    try:
        pins = dependency_pins(distributions, expected_yaga_version=expected_yaga_version)
        evidence["dependencies"] = pins
        evidence["excluded_distribution"] = {"name": "yaga-cli", "version": expected_yaga_version}
        with tempfile.TemporaryDirectory(prefix="dependency-audit-", dir=workspace) as temporary:
            directory = Path(temporary)
            environment = audit_environment(directory, dict(os.environ))
            command = [sys.executable, "-I", "-m", "pip_audit"]
            version = run_bounded_process(
                [*command, "--version"],
                cwd=directory,
                environment=environment,
                stdout_limit=1024,
                stderr_limit=MAX_AUDIT_ERROR_BYTES,
                timeout_seconds=30,
            )
            if (
                _process_failed(version)
                or version.returncode != 0
                or version.stderr
                or version.stdout.strip() != f"pip-audit {AUDITOR_VERSION}".encode()
            ):
                raise ValueError("the locked pip-audit version is unavailable")
            evidence["auditor_version"] = AUDITOR_VERSION
            requirements = directory / "requirements.txt"
            requirements.write_text(
                "".join(f"{name}=={value}\n" for name, value in pins.items()), encoding="utf-8"
            )
            result = run_bounded_process(
                [
                    *command,
                    "--requirement",
                    str(requirements),
                    "--strict",
                    "--no-deps",
                    "--disable-pip",
                    "--vulnerability-service",
                    "pypi",
                    "--index-url",
                    "https://pypi.org/simple",
                    "--cache-dir",
                    str(directory / "cache"),
                    "--progress-spinner",
                    "off",
                    "--timeout",
                    "15",
                    "--format",
                    "json",
                    "--desc",
                    "off",
                ],
                cwd=directory,
                environment=environment,
                stdout_limit=MAX_AUDIT_BYTES,
                stderr_limit=MAX_AUDIT_ERROR_BYTES,
                timeout_seconds=180,
            )
            evidence.update(
                {
                    "exit_code": result.returncode,
                    "stdout": result.stdout.decode("utf-8", errors="replace"),
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                    "timed_out": result.timed_out,
                    "stdout_overflow": result.stdout_overflow,
                    "stderr_overflow": result.stderr_overflow,
                }
            )
            if _process_failed(result):
                raise ValueError("dependency audit exceeded its process bounds")
            evidence.update(
                validate_audit_response(result.stdout, pins, returncode=result.returncode)
            )
            if evidence["status"] != "passed":
                raise DependencyAuditError(
                    "dependency audit reported known vulnerabilities", evidence
                )
        return evidence
    except (OSError, ValueError) as error:
        evidence["status"] = "error"
        evidence["error"] = str(error)
        raise DependencyAuditError(
            "dependency audit could not establish a clean result", evidence
        ) from error
    finally:
        evidence["completed_at_utc"] = datetime.now(UTC).isoformat()
