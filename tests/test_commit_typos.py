"""Tests for the bounded optional Typos adapter."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from yaga.commits.typos import MAX_CORRECTION_BYTES, MAX_CORRECTIONS, check_typos
from yaga.errors import InputError


def test_typos_pass_has_no_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    monkeypatch.setattr(
        "yaga.commits.typos.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, b"", b""),
    )

    assert check_typos("feat: add a clear description") == ()


def test_typos_runs_from_the_selected_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    calls: list[dict[str, object]] = []

    def run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(["typos"], 0, b"", b"")

    monkeypatch.setattr("yaga.commits.typos.subprocess.run", run)
    repository = Path("C:/checked-out/repository")

    assert check_typos("feat: add a clear description", repository=repository) == ()
    assert calls[0]["cwd"] == str(repository)


def test_typos_json_findings_become_stable_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    output = b'{"type":"typo","path":"-","line_num":2,"byte_offset":3,"typo":"teh","corrections":["the"]}\n'
    monkeypatch.setattr(
        "yaga.commits.typos.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 2, output, b""),
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
        "yaga.commits.typos.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 2, b"not-json\n", b""),
    )

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
        "yaga.commits.typos.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 2, output, b""),
    )

    with pytest.raises(InputError, match="malformed JSON"):
        check_typos("feat: add a description")


def test_typos_accepts_findings_without_a_byte_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yaga.commits.typos.shutil.which", lambda _name: "C:/bin/typos.exe")
    output = b'{"type":"typo","line_num":1,"typo":"teh","corrections":["the"]}\n'
    monkeypatch.setattr(
        "yaga.commits.typos.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 2, output, b""),
    )

    assert check_typos("teh")[0].column == 1
