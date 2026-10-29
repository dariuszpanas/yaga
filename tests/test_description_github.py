"""Explicit remote PR selection and bounded credential-safe gh response handling."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from yaga.descriptions import github
from yaga.descriptions.github import (
    FetchedPullRequestDescription,
    PullRequestReference,
    fetch_pull_request_description,
    parse_pull_request_reference,
)
from yaga.errors import InputError
from yaga.git.runtime import ProcessResult

REFERENCE = PullRequestReference("github.com", "example", "project", 7)


def _payload(**changes: Any) -> bytes:
    document = {"body": "## Summary\nExplain the change.", "number": 7, "url": REFERENCE.url}
    document.update(changes)
    return json.dumps(document, ensure_ascii=True).encode("utf-8")


def _mock_gh(
    monkeypatch: pytest.MonkeyPatch,
    result: ProcessResult | None = None,
) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(github.shutil, "which", lambda _name: sys.executable)

    def run(command: list[str], **options: Any) -> ProcessResult:
        calls.append((command, options))
        return result or ProcessResult(0, _payload(), b"", False, False, False)

    monkeypatch.setattr(github, "run_bounded_process", run)
    return calls


@pytest.mark.parametrize(
    ("value", "repository", "expected"),
    [
        ("7", "example/project", REFERENCE),
        ("7", "github.com/example/project", REFERENCE),
        (
            "7",
            "GIT.EXAMPLE.COM/example/project",
            PullRequestReference("git.example.com", "example", "project", 7),
        ),
        (REFERENCE.url, None, REFERENCE),
        (REFERENCE.url + "/", None, REFERENCE),
        (
            "https://GITHUB.COM/Example/Project/pull/7",
            None,
            PullRequestReference("github.com", "Example", "Project", 7),
        ),
        (
            "https://git.example.com/example/.github/pull/9",
            None,
            PullRequestReference("git.example.com", "example", ".github", 9),
        ),
        (
            str(2**63 - 1),
            "example/project",
            PullRequestReference("github.com", "example", "project", 2**63 - 1),
        ),
    ],
    ids=[
        "number",
        "explicit-host",
        "enterprise",
        "url",
        "trailing-slash",
        "case",
        "dot-repository",
        "maximum-number",
    ],
)
def test_reference_selects_one_explicit_remote_identity(
    value: str, repository: str | None, expected: PullRequestReference
) -> None:
    selected = parse_pull_request_reference(value, repository)
    assert selected == expected
    assert selected.url.endswith(f"/pull/{expected.number}")
    assert selected.gh_repository == f"{expected.host}/{expected.owner}/{expected.repository}"


@pytest.mark.parametrize(
    ("value", "repository"),
    [
        ("7", None),
        ("0", "example/project"),
        ("01", "example/project"),
        ("-7", "example/project"),
        ("+7", "example/project"),
        ("7.0", "example/project"),
        ("٧", "example/project"),
        (str(2**63), "example/project"),
        ("9" * 1025, "example/project"),
        ("main", "example/project"),
        ("--web", None),
        ("7\n--web", "example/project"),
        (" 7", "example/project"),
        ("7\x00", "example/project"),
        (REFERENCE.url, "example/project"),
        (REFERENCE.url, ""),
        ("http://github.com/example/project/pull/7", None),
        ("https://name@github.com/example/project/pull/7", None),
        ("https://name:password@github.com/example/project/pull/7", None),
        ("https://github.com:443/example/project/pull/7", None),
        ("https://github.com:/example/project/pull/7", None),
        ("https://github.com/example/project/pull/7?x=1", None),
        ("https://github.com/example/project/pull/7?", None),
        ("https://github.com/example/project/pull/7#issuecomment-1", None),
        ("https://github.com/example/project/pull/7#", None),
        ("https://github.com/example/project/pull/07", None),
        ("https://github.com/example/project/pull/7/files", None),
        ("https://github.com/example/project/issues/7", None),
        ("https://github.com/example/project/pull/7//", None),
        ("https://github.com/example/pro%6aect/pull/7", None),
        ("https://github.com/example/project/pull/%37", None),
        ("https://github.com/example/project/pull/7\r", None),
        ("https://github.com/example/project/pull/7\t", None),
        ("https://github.com./example/project/pull/7", None),
        ("https://[::1]/example/project/pull/7", None),
        ("https://github_com/example/project/pull/7", None),
        ("https://-github.com/example/project/pull/7", None),
        ("https://github..com/example/project/pull/7", None),
        ("https://github.com/example/../pull/7", None),
        ("https://github.com/example/project/pull/9223372036854775808", None),
        ("7", ""),
        ("7", "example"),
        ("7", "example/project/extra/more"),
        ("7", "example/project/"),
        ("7", "https://github.com/example/project"),
        ("7", "github.com:443/example/project"),
        ("7", "example/.."),
        ("7", "example/project --web"),
        ("7", "example/project\n"),
        ("7", "example/" + "x" * 101),
        ("7", "x" * 101 + "/project"),
        ("7", "a" * 64 + ".com/example/project"),
        ("7", ("a." * 127) + "com/example/project"),
    ],
)
def test_ambiguous_or_unsafe_selectors_are_input_errors(value: str, repository: str | None) -> None:
    with pytest.raises(InputError):
        parse_pull_request_reference(value, repository)


@pytest.mark.parametrize("value", [None, 7, True, [], {}])
def test_nonstring_selectors_are_rejected(value: Any) -> None:
    with pytest.raises(InputError):
        parse_pull_request_reference(value)


@pytest.mark.parametrize("number", [True, False, 0, -1, 2**63, "7", 7.0])
def test_reference_model_does_not_admit_invalid_numbers(number: Any) -> None:
    with pytest.raises(ValueError):
        PullRequestReference("github.com", "example", "project", number)


def test_fetch_uses_fixed_shell_free_bounded_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_gh(monkeypatch)

    fetched = fetch_pull_request_description(REFERENCE)

    assert fetched == FetchedPullRequestDescription(
        "## Summary\nExplain the change.", 7, REFERENCE.url
    )
    command, options = calls[0]
    assert command == [
        str(Path(sys.executable).resolve()),
        "pr",
        "view",
        "7",
        "--repo",
        "github.com/example/project",
        "--json",
        "body,number,url",
    ]
    assert options["stdout_limit"] == 512 * 1024
    assert options["stderr_limit"] == 64 * 1024
    assert options["timeout_seconds"] == 30
    assert "stdin_data" not in options
    assert "cwd" not in options
    assert len(calls) == 1


def test_fetch_preserves_auth_but_disables_ambient_targeting_logging_and_interaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("GH_DEBUG", "DEBUG", "GH_FORCE_TTY", "GH_HOST", "GH_REPO"):
        monkeypatch.setenv(key, "unwanted-override")
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_CONFIG_DIR"):
        monkeypatch.setenv(key, "test-auth-setting")
    monkeypatch.setenv("GH_TELEMETRY", "log")
    monkeypatch.setenv("GH_PAGER", "unsafe-pager")
    monkeypatch.setenv("PAGER", "unsafe-pager")
    calls = _mock_gh(monkeypatch)

    fetch_pull_request_description(REFERENCE)

    environment = calls[0][1]["environment"]
    for key in ("GH_DEBUG", "DEBUG", "GH_FORCE_TTY", "GH_HOST", "GH_REPO"):
        assert key not in environment
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_CONFIG_DIR"):
        assert environment[key] == "test-auth-setting"
    for key in ("GH_PROMPT_DISABLED", "GH_NO_UPDATE_NOTIFIER", "GH_NO_EXTENSION_UPDATE_NOTIFIER"):
        assert environment[key] == "1"
    assert environment["GH_PAGER"] == environment["PAGER"] == ""
    assert environment["GH_TELEMETRY"] == "false"
    assert "test-auth-setting" not in repr(calls[0][0])
    assert os.environ["GH_HOST"] == "unwanted-override"


@pytest.mark.parametrize(
    "body", [None, "", "## Summary\r\nPreserve `字` and 👩‍💻.\n", "Bad\rtext\t\x08"]
)
def test_fetch_preserves_exact_decoded_body_for_shared_validation(
    monkeypatch: pytest.MonkeyPatch, body: str | None
) -> None:
    _mock_gh(monkeypatch, ProcessResult(0, _payload(body=body), b"", False, False, False))
    assert fetch_pull_request_description(REFERENCE).body == (body or "")


def test_response_identity_accepts_case_changes_but_preserves_returned_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_gh(
        monkeypatch,
        ProcessResult(
            0, _payload(url="https://GITHUB.COM/Example/Project/pull/7/"), b"", False, False, False
        ),
    )
    assert (
        fetch_pull_request_description(REFERENCE).url == "https://github.com/Example/Project/pull/7"
    )


@pytest.mark.parametrize(
    "output",
    [
        b"not-json",
        b"\xff",
        b"null",
        b"[]",
        b"{}",
        b'{"body":"first","body":"second","number":7,"url":"https://github.com/example/project/pull/7"}',
        _payload(extra="not requested"),
        b'{"body":"text","number":7}',
        _payload(number=True),
        _payload(number="7"),
        _payload(number=7.0),
        _payload(number=8),
        _payload(body=[]),
        _payload(body=7),
        _payload(body="\ud800"),
        _payload(url=None),
        _payload(url=7),
        _payload(url="not-url"),
        _payload(url="https://github.com/example/project/pull/8"),
        _payload(url="https://github.com/different/project/pull/7"),
        _payload(url="https://github.com/example/different/pull/7"),
        _payload(url="https://different.example/example/project/pull/7"),
        _payload(url="https://github.com/example/project/pull/7?query"),
        b'{"body":NaN,"number":7,"url":"https://github.com/example/project/pull/7"}',
        b'{"body":"text","number":' + b"9" * 5000 + b"}",
        b"[" * 2000 + b"0" + b"]" * 2000,
    ],
)
def test_invalid_or_mismatched_remote_documents_fail_closed(
    monkeypatch: pytest.MonkeyPatch, output: bytes
) -> None:
    _mock_gh(monkeypatch, ProcessResult(0, output, b"", False, False, False))
    with pytest.raises(InputError):
        fetch_pull_request_description(REFERENCE)


def test_remote_body_byte_limit_preserves_exact_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "字" * 21845 + "a"
    assert len(body.encode()) == 65536
    _mock_gh(monkeypatch, ProcessResult(0, _payload(body=body), b"", False, False, False))
    assert fetch_pull_request_description(REFERENCE).body == body
    _mock_gh(monkeypatch, ProcessResult(0, _payload(body=body + "a"), b"", False, False, False))
    with pytest.raises(InputError, match="65536-byte"):
        fetch_pull_request_description(REFERENCE)


@pytest.mark.parametrize(
    "result",
    [
        ProcessResult(1, b"secret body", b"secret stderr", False, False, False),
        ProcessResult(4, b"secret body", b"secret auth", False, False, False),
        ProcessResult(0, b"secret body", b"secret stderr", True, False, False),
        ProcessResult(0, b"secret body", b"secret stderr", False, True, False),
        ProcessResult(0, b"secret body", b"secret stderr", False, False, True),
        ProcessResult(0, b"x" * (512 * 1024 + 1), b"", False, False, False),
    ],
    ids=["failure", "auth", "stdout-overflow", "stderr-overflow", "timeout", "oversized-json"],
)
def test_operational_failures_never_echo_gh_output(
    monkeypatch: pytest.MonkeyPatch, result: ProcessResult
) -> None:
    _mock_gh(monkeypatch, result)
    with pytest.raises(InputError) as failure:
        fetch_pull_request_description(REFERENCE)
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "error", [OSError("secret low-level details"), ValueError("secret values")]
)
def test_process_exceptions_do_not_expose_credential_or_transport_details(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _mock_gh(monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> ProcessResult:
        raise error

    monkeypatch.setattr(github, "run_bounded_process", fail)
    with pytest.raises(InputError) as failure:
        fetch_pull_request_description(REFERENCE)
    assert "secret" not in str(failure.value)
    assert failure.value.__suppress_context__


def test_missing_gh_has_an_actionable_installation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_gh(monkeypatch)
    monkeypatch.setattr(github.shutil, "which", lambda _name: None)
    with pytest.raises(InputError, match="install gh and authenticate"):
        fetch_pull_request_description(REFERENCE)
    assert calls == []


@pytest.mark.parametrize("selection", ["relative", "missing", "directory"])
def test_executable_must_be_absolute_existing_and_regular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    calls = _mock_gh(monkeypatch)
    executable = {
        "relative": "gh.exe",
        "missing": str(tmp_path / "absent.exe"),
        "directory": str(tmp_path),
    }[selection]
    monkeypatch.setattr(github.shutil, "which", lambda _name: executable)
    with pytest.raises(InputError, match="absolute regular executable"):
        fetch_pull_request_description(REFERENCE)
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="native Windows shell-dispatch boundary")
@pytest.mark.parametrize("suffix", [".cmd", ".bat", ".com", ".ps1"])
def test_windows_script_and_non_exe_discovery_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    calls = _mock_gh(monkeypatch)
    executable = tmp_path / ("gh" + suffix)
    executable.write_text("unused", encoding="utf-8")
    monkeypatch.setattr(github.shutil, "which", lambda _name: str(executable))
    with pytest.raises(InputError, match="gh.exe on Windows"):
        fetch_pull_request_description(REFERENCE)
    assert calls == []


def test_current_directory_cannot_supply_the_credential_receiving_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _mock_gh(monkeypatch)
    executable = tmp_path / ("gh.exe" if os.name == "nt" else "gh")
    executable.write_bytes(b"unused")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(github.shutil, "which", lambda _name: str(executable))

    with pytest.raises(InputError, match="outside the current directory"):
        fetch_pull_request_description(REFERENCE)
    assert calls == []


def test_absolute_path_installation_beneath_cwd_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _mock_gh(monkeypatch)
    tools = tmp_path / "tools"
    tools.mkdir()
    executable = tools / ("gh.exe" if os.name == "nt" else "gh")
    executable.write_bytes(b"unused")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(github.shutil, "which", lambda _name: str(executable))

    assert fetch_pull_request_description(REFERENCE).number == 7
    assert calls[0][0][0] == str(executable)


def test_success_stderr_is_never_reported_as_description_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_gh(monkeypatch, ProcessResult(0, _payload(), b"untrusted warning", False, False, False))
    assert fetch_pull_request_description(REFERENCE).body == "## Summary\nExplain the change."
