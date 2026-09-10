"""Tests for the installed changed-path policy command and service."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.changes import service
from yaga.changes.models import (
    ChangePolicy,
    ChangeReport,
    ChangeRule,
    ChangeRuleResult,
    ChangeSelection,
    LoadedChangePolicy,
)
from yaga.changes.service import CheckedChanges
from yaga.cli import app
from yaga.commands import change as change_commands
from yaga.errors import GitError

runner = CliRunner()


def _unstyle(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def _selection(*paths: str) -> ChangeSelection:
    return ChangeSelection(
        revision_range="a" * 40 + "..." + "b" * 40,
        base_sha="a" * 40,
        head_sha="b" * 40,
        comparison_sha="c" * 40,
        paths=tuple(sorted(paths)),
    )


def _checked(tmp_path: Path, *, failed: bool) -> CheckedChanges:
    paths = ("src/main.py",) if failed else ("src/main.py", "tests/test_main.py")
    selection = _selection(*paths)
    rule = ChangeRule(
        name="source-needs-tests",
        when_any=("src/**/*.py",),
        require_any=("tests/**/*.py",),
    )
    result = ChangeRuleResult(
        rule=rule,
        triggered_paths=("src/main.py",),
        required_paths=() if failed else ("tests/test_main.py",),
    )
    return CheckedChanges(
        report=ChangeReport(selection=selection, results=(result,)),
        policy_path=(tmp_path / "change-policy.toml").resolve(),
    )


def test_change_help_exposes_only_explicit_policy_range_and_runtime_options() -> None:
    root = runner.invoke(app, ["--help"])
    group = runner.invoke(app, ["change", "--help"])
    command = runner.invoke(app, ["change", "check", "--help"])

    assert root.exit_code == 0
    assert "change" in root.stdout
    assert group.exit_code == 0
    assert "Check changed-path coupling policy" in group.stdout
    assert command.exit_code == 0
    help_text = _unstyle(command.stdout)
    for option in ("--policy", "--range", "--repo", "--format"):
        assert option in help_text
    for output_format in ("text", "json", "github"):
        assert output_format in help_text
    assert "--quiet" in help_text
    for unsupported in ("--staged", "--working-tree", "--token"):
        assert unsupported not in help_text


@pytest.mark.parametrize("missing", ["policy", "range"])
def test_change_check_requires_both_explicit_selections(missing: str) -> None:
    arguments = ["change", "check"]
    if missing != "policy":
        arguments.extend(("--policy", "change-policy.toml"))
    if missing != "range":
        arguments.extend(("--range", "main...HEAD"))

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert f"--{missing}" in _unstyle(result.stderr)


@pytest.mark.parametrize(
    ("failed", "expected_exit", "quiet"),
    [(False, 0, False), (True, 1, False), (False, 0, True), (True, 1, True)],
)
def test_change_check_reports_policy_outcomes_on_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
    expected_exit: int,
    quiet: bool,
) -> None:
    checked = _checked(tmp_path, failed=failed)
    captured: dict[str, object] = {}

    def fake_check_changes(
        repository: Path,
        *,
        policy_path: Path,
        revision_range: str,
    ) -> CheckedChanges:
        captured.update(
            repository=repository,
            policy_path=policy_path,
            revision_range=revision_range,
        )
        return checked

    monkeypatch.setattr(change_commands, "check_changes", fake_check_changes)
    result = runner.invoke(
        app,
        [
            "change",
            "check",
            "--policy",
            "policy.toml",
            "--range",
            "main...HEAD",
            "--repo",
            str(tmp_path),
            "--format",
            "json",
            *(["--quiet"] if quiet else []),
        ],
    )

    assert result.exit_code == expected_exit, result.stderr
    assert result.stderr == ""
    if quiet:
        assert result.stdout == ""
    else:
        document = json.loads(result.stdout)
        assert document["status"] == ("failed" if failed else "passed")
    assert captured == {
        "repository": tmp_path,
        "policy_path": Path("policy.toml"),
        "revision_range": "main...HEAD",
    }


@pytest.mark.parametrize("output_format", ["text", "json", "github"])
def test_change_check_renders_operational_errors_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.setattr(
        change_commands,
        "check_changes",
        lambda *args, **kwargs: (_ for _ in ()).throw(GitError("bad\x1b[31m\nrange")),
    )

    result = runner.invoke(
        app,
        [
            "change",
            "check",
            "--policy",
            "policy.toml",
            "--range",
            "main...HEAD",
            "--format",
            output_format,
            "--quiet",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "\x1b" not in result.stderr
    if output_format == "json":
        document = json.loads(result.stderr)
        assert document["status"] == "error"
        assert document["error"] == {"kind": "git", "message": "bad?[31m?range"}
    elif output_format == "github":
        assert result.stderr.strip() == (
            "::error title=YAGA change policy::YAGA git error: bad?[31m?range"
        )
    else:
        assert result.stderr.strip() == "YAGA git error: bad?[31m?range"


def test_change_service_composes_loader_git_and_checker_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = (tmp_path / "policy.toml").resolve()
    policy = ChangePolicy(
        change_policy_version=1,
        rules=(
            ChangeRule(
                name="source-needs-tests",
                when_any=("src/**/*.py",),
                require_any=("tests/**/*.py",),
            ),
        ),
    )
    loaded = LoadedChangePolicy(policy=policy, path=policy_path)
    selection = _selection("src/main.py")
    report = ChangeReport(
        selection=selection,
        results=(
            ChangeRuleResult(
                rule=policy.rules[0],
                triggered_paths=("src/main.py",),
                required_paths=(),
            ),
        ),
    )
    calls: list[tuple[object, ...]] = []

    def fake_load(selected_path: Path) -> LoadedChangePolicy:
        calls.append(("load", selected_path))
        return loaded

    def fake_read(repository: Path, revision_range: str) -> ChangeSelection:
        calls.append(("git", repository, revision_range))
        return selection

    def fake_check(
        selected_policy: ChangePolicy,
        selected_paths: ChangeSelection,
    ) -> ChangeReport:
        calls.append(("check", selected_policy, selected_paths))
        return report

    monkeypatch.setattr(service, "load_change_policy", fake_load)
    monkeypatch.setattr(service, "read_changed_paths", fake_read)
    monkeypatch.setattr(service, "check_changed_paths", fake_check)

    checked = service.check_changes(
        tmp_path,
        policy_path=Path("policy.toml"),
        revision_range="main...HEAD",
    )

    assert checked == CheckedChanges(report=report, policy_path=policy_path)
    assert calls == [
        ("load", Path("policy.toml")),
        ("git", tmp_path, "main...HEAD"),
        ("check", policy, selection),
    ]
