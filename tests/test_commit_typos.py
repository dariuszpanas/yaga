"""Tests for the bounded optional Typos adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yaga.commits.typos import MAX_CORRECTION_BYTES, MAX_CORRECTIONS, check_typos
from yaga.errors import InputError
from yaga.git.runtime import ProcessResult


def test_typos_pass_has_no_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *args, **kwargs: ProcessResult(0, b"", b"", False, False, False),
    )

    assert check_typos("feat: add a clear description") == ()


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (0, b"not-json\n", b""),
        (
            0,
            b'{"type":"typo","line_num":1,"typo":"teh","corrections":["the"]}\n',
            b"",
        ),
        (0, b"", b"unexpected warning"),
        (
            2,
            b'{"type":"typo","line_num":1,"typo":"teh","corrections":["the"]}\n',
            b"partial failure",
        ),
    ],
    ids=[
        "clean-with-malformed-output",
        "clean-with-findings",
        "clean-with-stderr",
        "findings-with-stderr",
    ],
)
def test_typos_status_and_output_must_agree(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: bytes, stderr: bytes
) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *_args, **_kwargs: ProcessResult(returncode, stdout, stderr, False, False, False),
    )

    with pytest.raises(InputError, match="unexpected"):
        check_typos("feat: fix teh spelling")


def test_typos_runs_from_the_selected_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    calls: list[dict[str, object]] = []

    def run(*_args: object, **kwargs: object) -> ProcessResult:
        calls.append(kwargs)
        return ProcessResult(0, b"", b"", False, False, False)

    monkeypatch.setattr("yaga.commits.typos.run_bounded_process", run)
    repository = Path("C:/checked-out/repository")

    assert check_typos("feat: add a clear description", repository=repository) == ()
    assert calls[0]["cwd"] == repository
    assert calls[0]["stdin_data"] == b"feat: add a clear description"
    assert calls[0]["stdout_limit"] == calls[0]["stderr_limit"] == 64 * 1024
    assert calls[0]["timeout_seconds"] == 5


def test_typos_json_findings_become_stable_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    output = b'{"type":"typo","path":"-","line_num":2,"byte_offset":3,"typo":"teh","corrections":["the"]}\n'
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *args, **kwargs: ProcessResult(2, output, b"", False, False, False),
    )

    diagnostics = check_typos("feat: add a description\n\nteh")

    assert diagnostics[0].code == "typos.word"
    assert diagnostics[0].line == 2
    assert diagnostics[0].column == 4
    assert "teh" in diagnostics[0].message
    assert "the" in diagnostics[0].message


def test_typos_is_required_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: None)

    with pytest.raises(InputError, match="typos executable"):
        check_typos("feat: add a description")


def test_typos_rejects_malformed_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *args, **kwargs: ProcessResult(2, b"not-json\n", b"", False, False, False),
    )

    with pytest.raises(InputError, match="JSON"):
        check_typos("feat: add a description")


@pytest.mark.parametrize(
    "fields",
    [
        '"type":"error","type":"typo"',
        '"type":"typo","context":{"word":"first","word":"second"}',
        '"type":"typo","context":NaN',
        '"type":"typo","context":Infinity',
        '"type":"typo","context":-Infinity',
    ],
    ids=["duplicate-type", "duplicate-nested-field", "nan", "infinity", "negative-infinity"],
)
def test_typos_rejects_ambiguous_and_nonfinite_json(
    monkeypatch: pytest.MonkeyPatch, fields: str
) -> None:
    output = ("{" + fields + ',"line_num":1,"typo":"teh","corrections":["the"]}\n').encode()
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *_args, **_kwargs: ProcessResult(2, output, b"", False, False, False),
    )

    with pytest.raises(InputError, match="malformed JSON"):
        check_typos("feat: fix teh spelling")


def test_typos_retains_valid_unknown_provider_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    output = (
        b'{"type":"typo","line_num":1,"byte_offset":10,"typo":"teh",'
        b'"corrections":["the"],"path":"-","context":{"count":1,"ratio":0.5}}\n'
    )
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *_args, **_kwargs: ProcessResult(2, output, b"", False, False, False),
    )

    findings = check_typos("feat: fix teh spelling")

    assert len(findings) == 1
    assert findings[0].code == "typos.word"
    assert findings[0].line == 1
    assert findings[0].column == 11


@pytest.mark.parametrize(
    "output",
    [
        b"[" * 2000 + b"0" + b"]" * 2000,
        b'{"type":"typo","line_num":' + b"9" * 5000 + b"}",
        b'{"type":"typo","line_num":1,"typo":"\\ud800","corrections":["the"]}',
        b'{"type":"typo","line_num":1,"typo":"teh","corrections":["\\ud800"]}',
    ],
    ids=["nested-json", "large-integer", "surrogate-typo", "surrogate-correction"],
)
def test_typos_normalizes_unrepresentable_json_findings_to_input_error(
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *_args, **_kwargs: ProcessResult(2, output, b"", False, False, False),
    )

    with pytest.raises(InputError, match="JSON"):
        check_typos("feat: add a description")


def test_typos_normalizes_json_decoder_recursion_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *_args, **_kwargs: ProcessResult(2, b"{}", b"", False, False, False),
    )

    def reject_recursion(_line: str, **_options: object) -> object:
        raise RecursionError("decoder nesting limit")

    monkeypatch.setattr("yaga.commits.typos.json.loads", reject_recursion)
    with pytest.raises(InputError, match="malformed JSON"):
        check_typos("feat: add a description")


@pytest.mark.parametrize(
    "finding",
    [
        {
            "type": "typo",
            "line_num": 1,
            "typo": "teh",
            "corrections": ["the"] * (MAX_CORRECTIONS + 1),
        },
        {
            "type": "typo",
            "line_num": 1,
            "typo": "teh",
            "corrections": ["x" * (MAX_CORRECTION_BYTES + 1)],
        },
    ],
)
def test_typos_rejects_oversized_finding_fields(
    monkeypatch: pytest.MonkeyPatch, finding: dict[str, object]
) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    output = (json.dumps(finding) + "\n").encode()
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *args, **kwargs: ProcessResult(2, output, b"", False, False, False),
    )

    with pytest.raises(InputError, match="malformed JSON"):
        check_typos("feat: add a description")


def test_typos_accepts_findings_without_a_byte_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    output = b'{"type":"typo","line_num":1,"typo":"teh","corrections":["the"]}\n'
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *args, **kwargs: ProcessResult(2, output, b"", False, False, False),
    )

    assert check_typos("teh")[0].column == 1


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (ProcessResult(0, b"", b"", True, False, False), "output limit"),
        (ProcessResult(0, b"", b"", False, True, False), "output limit"),
        (ProcessResult(0, b"", b"", False, False, True), "did not complete within"),
    ],
)
def test_typos_process_limits_are_operational_errors(
    monkeypatch: pytest.MonkeyPatch,
    result: ProcessResult,
    message: str,
) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    monkeypatch.setattr("yaga.commits.typos.run_bounded_process", lambda *_args, **_kwargs: result)

    with pytest.raises(InputError, match=message):
        check_typos("feat: add a description")


def test_typos_normalizes_line_endings_before_spelling_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")

    def run(*_args: object, **kwargs: object) -> ProcessResult:
        assert kwargs["stdin_data"] == b"feat: add behavior\n\nteh issue"
        output = (
            b'{"type":"typo","line_num":3,"byte_offset":0,"typo":"teh","corrections":["the"]}\n'
        )
        return ProcessResult(2, output, b"", False, False, False)

    monkeypatch.setattr("yaga.commits.typos.run_bounded_process", run)

    assert check_typos("feat: add behavior\r\rteh issue")[0].line == 3


@pytest.mark.parametrize(
    "message",
    ["\ud800", "x" * (1024 * 1024 + 1)],
    ids=["surrogate", "oversized"],
)
def test_typos_rejects_invalid_or_oversized_input_before_launch(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process",
        lambda *_args, **_kwargs: pytest.fail("invalid input must not start a process"),
    )

    with pytest.raises(InputError, match="commit message"):
        check_typos(message)


@pytest.mark.parametrize("isolated", [False, True])
def test_typos_isolation_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated: bool
) -> None:
    executable = tmp_path / "typos"
    executable.touch()
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: str(executable))

    def run(command, **kwargs):
        assert ("--isolated" in command) is isolated
        assert kwargs["cwd"] == repo
        return ProcessResult(0, b"", b"", False, False, False)

    monkeypatch.setattr("yaga.commits.typos.run_bounded_process", run)
    assert check_typos("fix: correct spelling", repository=repo, isolated=isolated) == ()


def test_trusted_typos_rejects_repository_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "typos"
    executable.touch()
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: str(executable))
    monkeypatch.setattr(
        "yaga.commits.typos.run_bounded_process", lambda *a, **kw: pytest.fail("must not execute")
    )
    with pytest.raises(InputError, match="outside the repository"):
        check_typos("fix: correct spelling", repository=tmp_path, isolated=True)


def test_local_config_isolation_allows_project_installed_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "typos"
    executable.touch()
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: str(executable))

    def run(command, **kwargs):
        assert "--isolated" in command
        return ProcessResult(0, b"", b"", False, False, False)

    monkeypatch.setattr("yaga.commits.typos.run_bounded_process", run)
    assert check_typos("fix: correct spelling", repository=tmp_path, config_isolated=True) == ()
