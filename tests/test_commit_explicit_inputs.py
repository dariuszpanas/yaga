"""Public title, editor input, and spelling configuration boundaries."""

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commits.config import load_config
from yaga.commits.models import TyposConfig
from yaga.commits.service import check_commits
from yaga.errors import ConfigurationError, GitError, InputError
from yaga.git.runtime import ProcessResult


def policy(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / ".yaga.toml"
    path.write_text(
        'config-version = 1\n[commit]\nbody-policy = "required"\n'
        'required-footer-tokens = ["Validation"]\n' + extra,
        encoding="utf-8",
    )
    return path


def test_title_skips_only_full_message_policy(tmp_path: Path) -> None:
    config = policy(tmp_path)
    args = ["commit", "check", "--repo", str(tmp_path), "--config", str(config)]
    runner = CliRunner()
    assert runner.invoke(app, [*args, "--title", "fix: correct the result"]).exit_code == 0
    assert runner.invoke(app, [*args, "--title", "invalid title"]).exit_code == 1
    assert runner.invoke(app, [*args, "--message", "fix: correct the result"]).exit_code == 1
    assert runner.invoke(app, [*args, "--title"]).exit_code == 2


@pytest.mark.parametrize("title", ["", "fix: title\nbody", "fix: title\r", "fix: \0", "x" * 1025])
def test_title_rejects_invalid_input(tmp_path: Path, title: str) -> None:
    with pytest.raises(InputError):
        check_commits(tmp_path, title=title, config=policy(tmp_path))


@pytest.mark.parametrize("source", ["message", "file", "stdin", "commit", "revision_range", "edit"])
def test_title_cannot_mix_sources(tmp_path: Path, source: str) -> None:
    with pytest.raises(InputError, match="choose only one"):
        check_commits(
            tmp_path,
            title="fix: title",
            message="fix: other" if source == "message" else None,
            file=tmp_path / "msg" if source == "file" else None,
            edit=tmp_path / "msg" if source == "edit" else None,
            stdin=source == "stdin",
            commit="HEAD" if source == "commit" else None,
            revision_range="HEAD~1..HEAD" if source == "revision_range" else None,
        )


def test_editor_comments_cannot_supply_required_body(tmp_path: Path) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.commentChar", ";"], check=True)
    config = policy(tmp_path)
    message = tmp_path / "COMMIT_EDITMSG"
    original = "fix: correct the result\n\n; Guidance is not prose.\n\nValidation: tested\n"
    message.write_text(original, encoding="utf-8")
    assert check_commits(tmp_path, file=message, config=config).valid
    report = check_commits(tmp_path, edit=message, config=config)
    assert [item.code for item in report.results[0].diagnostics] == ["body.required"]
    assert message.read_text(encoding="utf-8") == original
    message.write_text(original.replace("; Guidance", "Actual guidance"), encoding="utf-8")
    assert check_commits(tmp_path, edit=message, config=config).valid
    message.write_bytes(b"\xff")
    with pytest.raises(InputError, match="UTF-8"):
        check_commits(tmp_path, edit=message, config=config)


def test_editor_uses_global_comment_settings_with_project_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    global_config = tmp_path / "user.gitconfig"
    global_config.write_text('[core]\ncommentChar = ";"\n', encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    config = policy(tmp_path)
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("fix: result\n\n; Not prose\n\nValidation: checked\n", encoding="utf-8")
    assert not check_commits(tmp_path, edit=message, config=config).valid
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.commentChar", "#"], check=True)
    assert check_commits(tmp_path, edit=message, config=config).valid
    message.unlink()
    with pytest.raises(InputError, match="cannot read"):
        check_commits(tmp_path, edit=message, config=config)


@pytest.mark.parametrize("value", ['"isolated"', '"repository"', '"unknown"', "true"])
def test_spelling_configuration_is_closed(tmp_path: Path, value: str) -> None:
    config = policy(tmp_path, f"typos-config = {value}\n")
    if value in {'"isolated"', '"repository"'}:
        assert load_config(config).policy.typos_config == TyposConfig(value.strip('"'))
    else:
        with pytest.raises(ConfigurationError):
            load_config(config)


@pytest.mark.skipif(shutil.which("typos") is None, reason="Typos CLI unavailable")
def test_title_spelling_ignores_ambient_exemption_when_isolated(tmp_path: Path) -> None:
    config = policy(tmp_path, 'typos = "check"\ntypos-config = "isolated"\n')
    (tmp_path / "_typos.toml").write_text("[default]\ncheck-file = false\n", encoding="utf-8")
    assert check_commits(tmp_path, title="fix: correct the result", config=config).valid
    report = check_commits(tmp_path, title="fix: correct teh result", config=config)
    assert [item.code for item in report.results[0].diagnostics] == ["typos.word"]


@pytest.mark.parametrize(
    "result",
    [
        ProcessResult(1, b"", b"invalid configuration", False, False, False),
        ProcessResult(0, b"", b"", True, False, False),
        ProcessResult(0, b"", b"", False, True, False),
        ProcessResult(0, b"", b"", False, False, True),
    ],
)
def test_editor_cleanup_errors_never_fall_back_to_raw_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: ProcessResult
) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("fix: result\n\n; Comment\n", encoding="utf-8")
    monkeypatch.setattr("yaga.git.runtime.run_bounded_process", lambda *args, **kwargs: result)
    with pytest.raises(GitError, match="editor cleanup"):
        check_commits(tmp_path, edit=message, config=policy(tmp_path))
