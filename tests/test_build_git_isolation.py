"""Installed-wheel fixture setup cannot mutate an ambient Git repository."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.check_build import run_smoke_git


def test_build_git_setup_preserves_ambient_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for the installed-wheel setup regression")
    other = tmp_path / "unrelated"
    consumer = tmp_path / "consumer"
    other.mkdir()
    consumer.mkdir()
    run_smoke_git(git, other, ["init", "--initial-branch=main", "--template="])
    run_smoke_git(git, other, ["config", "user.name", "Original Author"])
    run_smoke_git(git, other, ["config", "user.email", "original@example.invalid"])
    (other / "unrelated.txt").write_text("unrelated work\n", encoding="utf-8")
    original_config = (other / ".git/config").read_bytes()
    fake_index = tmp_path / "unrelated-index"
    template = tmp_path / "template"
    template.mkdir()
    (template / "injected-file").write_text("unexpected template\n", encoding="utf-8")
    config = tmp_path / "global.gitconfig"
    config.write_text("[user]\n\tname = Ambient Author\n", encoding="utf-8")
    for name, value in {
        "GIT_DIR": str(other / ".git"),
        "GIT_WORK_TREE": str(other),
        "GIT_INDEX_FILE": str(fake_index),
        "GIT_TEMPLATE_DIR": str(template),
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "user.name",
        "GIT_CONFIG_VALUE_0": "Injected Author",
    }.items():
        monkeypatch.setenv(name, value)
    run_smoke_git(git, consumer, ["init", "--initial-branch=main", "--template="])
    run_smoke_git(git, consumer, ["config", "user.name", "YAGA Build"])
    run_smoke_git(git, consumer, ["config", "user.email", "yaga-build@example.invalid"])
    (consumer / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    run_smoke_git(git, consumer, ["add", "--all"])
    run_smoke_git(
        git,
        consumer,
        ["commit", "--no-verify", "--no-gpg-sign", "--message", "test: create fixture"],
    )

    assert (other / ".git/config").read_bytes() == original_config
    assert not (other / ".git/index").exists()
    assert not fake_index.exists()
    assert not (consumer / ".git/injected-file").exists()
    assert run_smoke_git(git, consumer, ["log", "-1", "--format=%an"]) == "YAGA Build"
    assert run_smoke_git(git, consumer, ["ls-files"]) == "fixture.txt"
    clean_environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    status = subprocess.check_output(
        [git, "-C", str(other), "status", "--short"], env=clean_environment, text=True
    )
    assert status.strip() == "?? unrelated.txt"
