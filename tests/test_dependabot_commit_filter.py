"""Dependabot PR exemptions must not spread to ordinary human-added commits."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_github_pull_request import (
    DEPENDABOT_AUTHOR,
    _commit,
    _event_document,
    _git,
    _repository,
    _write_event,
    _write_skip_config,
)
from yaga.commits import dependabot, github_event
from yaga.commits.git import read_range
from yaga.commits.github_event import check_pull_request
from yaga.errors import GitError
from yaga.git.runtime import ProcessResult

BOT_EMAIL = "49699333+dependabot[bot]@users.noreply.github.com"
SIGNATURE_FIXTURES = Path(__file__).parent / "fixtures" / "github-signatures"
SIGNED_HEAD = "2dea34660d96cdc91ae89eb11b445f816fbf3cea"
SIGNED_PARENT = "646a0a1d28b404fabb79776612be4721c11052ff"


def _enable_gpgv(monkeypatch: pytest.MonkeyPatch) -> None:
    executable = shutil.which("gpgv")
    if executable is None and os.name == "nt":
        git = shutil.which("git")
        bundled = Path(git).parent.parent / "usr" / "bin" / "gpgv.exe" if git else None
        if bundled is not None and bundled.is_file():
            executable = str(bundled)
    if executable is None:
        pytest.skip("offline GitHub signature integration requires gpgv")
    monkeypatch.setenv("PATH", str(Path(executable).parent) + os.pathsep + os.environ["PATH"])


def _write_commit_object(repository: Path, raw: bytes) -> str:
    return (
        subprocess.run(
            ["git", "-C", str(repository), "hash-object", "-t", "commit", "-w", "--stdin"],
            input=raw,
            check=True,
            capture_output=True,
        )
        .stdout.decode("ascii")
        .strip()
    )


def _signed_repository(tmp_path: Path, *, addition: str = "none") -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    raw = (SIGNATURE_FIXTURES / "dependabot-urllib3.commit").read_bytes()
    parent = (SIGNATURE_FIXTURES / "dependabot-urllib3-parent.commit").read_bytes()
    assert _write_commit_object(repository, parent) == SIGNED_PARENT
    assert _write_commit_object(repository, raw) == SIGNED_HEAD
    head = SIGNED_HEAD
    if addition == "copied-signature":
        tampered = raw.replace(SIGNED_PARENT.encode(), SIGNED_HEAD.encode(), 1)
        head = _write_commit_object(repository, tampered + b"\nUntrusted addition.\n")
    elif addition == "human":
        # No tree content is needed for commit-message policy. Retain the original
        # tree ID while creating an unsigned human addition to the signed commit.
        tree = raw.split(b"\n", 1)[0]
        head = _write_commit_object(
            repository,
            tree
            + f"\nparent {SIGNED_HEAD}\n".encode()
            + b"author Human <human@example.invalid> 1791682551 +0000\n"
            + b"committer Human <human@example.invalid> 1791682551 +0000\n\nhuman repair\n",
        )
    _git(repository, "update-ref", "refs/heads/main", head)
    return repository, SIGNED_PARENT, head


@pytest.mark.parametrize("addition", ["none", "human", "copied-signature"])
def test_real_signed_bot_commit_is_skipped_but_added_commits_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, addition: str
) -> None:
    _enable_gpgv(monkeypatch)
    repository, base, head = _signed_repository(tmp_path, addition=addition)
    _write_skip_config(repository, extra='pull-request-message = "title-and-body"\n')
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(base, head, title="bad edited title", author=DEPENDABOT_AUTHOR),
    )

    report = check_pull_request(event_file, repository)

    results = {result.target.sha: result for result in report.commits}
    assert results[SIGNED_HEAD].skipped_reason == "Dependabot pull request"
    assert report.proposed_message is not None
    if addition == "none":
        assert report.valid
        assert report.skipped == 3
    else:
        assert results[head].skipped_reason is None
        assert report.skipped == 1
        assert report.title.status == "failed"
        assert report.proposed_message.status == "failed"


def test_explicit_signature_opt_out_does_not_invoke_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _, base = _repository(tmp_path)
    head = _bot_commit(repository)
    targets = read_range(repository, f"{base}..{head}", max_commits=2)

    def unexpected_verification(*args: object, **kwargs: object) -> frozenset[str]:
        raise AssertionError("explicit opt-out must not invoke signature verification")

    monkeypatch.setattr(dependabot, "verified_github_commits", unexpected_verification)

    assert dependabot.filter_dependabot_commits(
        repository,
        base_sha=base,
        head_sha=head,
        targets=targets,
        require_github_signature=False,
    ) == {head}


def _bot_commit(repository: Path, *, committer: str = "GitHub") -> str:
    _git(repository, "config", "user.name", committer)
    _git(
        repository,
        "config",
        "user.email",
        "noreply@github.com" if committer == "GitHub" else BOT_EMAIL,
    )
    _git(
        repository,
        "commit",
        "--allow-empty",
        "--author",
        f"dependabot[bot] <{BOT_EMAIL}>",
        "-m",
        "bot update without a conventional header",
    )
    return _git(repository, "rev-parse", "HEAD")


@pytest.mark.parametrize("later_bot_commit", [False, True])
@pytest.mark.parametrize("human_message", ["fix: repair compatibility", "human repair"])
def test_mixed_pr_checks_human_commits_even_after_another_bot_update(
    tmp_path: Path, later_bot_commit: bool, human_message: str
) -> None:
    repository, _, base = _repository(tmp_path)
    first_bot = _bot_commit(repository)
    _git(repository, "config", "user.name", "YAGA Tests")
    _git(repository, "config", "user.email", "yaga@example.invalid")
    human = _commit(repository, human_message, "human repair")
    head = _bot_commit(repository, committer="dependabot[bot]") if later_bot_commit else human
    _write_skip_config(
        repository,
        extra='pull-request-message = "title-and-body"\ndependabot-require-github-signature = false\n',
    )
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(base, head, title="bad edited title", author=DEPENDABOT_AUTHOR),
    )

    report = check_pull_request(event_file, repository)

    results = {result.target.sha: result for result in report.commits}
    assert results[first_bot].skipped_reason == "Dependabot pull request"
    assert results[human].skipped_reason is None
    assert results[human].status == ("passed" if human_message.startswith("fix:") else "failed")
    if later_bot_commit:
        assert results[head].skipped_reason == "Dependabot pull request"
    assert report.title.status == "failed"
    assert report.title.skipped_reason is None
    assert report.proposed_message is not None
    assert report.proposed_message.status == "failed"
    assert report.proposed_message.skipped_reason is None


@pytest.mark.parametrize("human_position", [0, 1, 2])
def test_human_commit_is_checked_at_every_position_despite_a_bot_signoff(
    tmp_path: Path, human_position: int
) -> None:
    repository, _, base = _repository(tmp_path)
    human = ""
    for position in range(3):
        if position == human_position:
            _git(repository, "config", "user.name", "YAGA Tests")
            _git(repository, "config", "user.email", "yaga@example.invalid")
            human = head = _commit(
                repository,
                "human repair\n\nSigned-off-by: dependabot[bot] <support@github.com>",
                "human repair",
            )
        else:
            head = _bot_commit(repository)
    _write_skip_config(repository, extra="dependabot-require-github-signature = false\n")
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(base, head, author=DEPENDABOT_AUTHOR),
    )

    report = check_pull_request(event_file, repository)

    assert [result.target.sha for result in report.commits if result.status == "failed"] == [human]
    assert report.skipped == 2
    assert report.title.skipped_reason is None


@pytest.mark.parametrize("committer", ["GitHub", "dependabot[bot]"])
def test_explicit_signature_opt_out_retains_metadata_title_and_commit_exemption(
    tmp_path: Path, committer: str
) -> None:
    repository, _, base = _repository(tmp_path)
    head = _bot_commit(repository, committer=committer)
    _write_skip_config(repository, extra="dependabot-require-github-signature = false\n")
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(base, head, title="bot title", author=DEPENDABOT_AUTHOR),
    )

    report = check_pull_request(event_file, repository)

    assert report.valid
    assert report.skipped == 2
    assert all(result.skipped_reason == "Dependabot pull request" for result in report.results)


def test_rebased_bot_authorship_with_human_committer_is_checked(tmp_path: Path) -> None:
    repository, _, base = _repository(tmp_path)
    head = _bot_commit(repository)
    _git(repository, "config", "user.name", "YAGA Tests")
    _git(repository, "config", "user.email", "yaga@example.invalid")
    _git(repository, "commit", "--amend", "--allow-empty", "--no-edit")
    head = _git(repository, "rev-parse", "HEAD")
    _write_skip_config(repository)
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(base, head, title="bot title", author=DEPENDABOT_AUTHOR),
    )

    report = check_pull_request(event_file, repository)

    assert report.failed == 2
    assert report.skipped == 0


def test_bot_looking_git_metadata_never_enables_exemption_for_a_human_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _, base = _repository(tmp_path)
    head = _bot_commit(repository)
    _git(
        repository,
        "commit",
        "--amend",
        "--allow-empty",
        "-m",
        "forged update\n\nSigned-off-by: dependabot[bot] <support@github.com>",
    )
    head = _git(repository, "rev-parse", "HEAD")
    _write_skip_config(repository)
    event_file = _write_event(
        tmp_path / "event.json", _event_document(base, head, title="bad title")
    )

    def unexpected_metadata(*args: object, **kwargs: object) -> frozenset[str]:
        raise AssertionError("metadata filtering must require eligible PR-author policy first")

    monkeypatch.setattr(github_event, "filter_dependabot_commits", unexpected_metadata)

    report = check_pull_request(event_file, repository)

    assert report.failed == 2
    assert report.skipped == 0


@pytest.mark.parametrize("signature_policy", ["", "dependabot-require-github-signature = true\n"])
def test_unsigned_forged_metadata_inside_an_eligible_bot_pr_is_checked(
    tmp_path: Path,
    signature_policy: str,
) -> None:
    repository, _, base = _repository(tmp_path)
    head = _bot_commit(repository)
    _write_skip_config(repository, extra=signature_policy)
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(base, head, title="forged title", author=DEPENDABOT_AUTHOR),
    )

    report = check_pull_request(event_file, repository)

    assert report.failed == 2
    assert report.skipped == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (1, b""),
        (1, b"Dependabot[bot]"),
        (2, b"dependabot@example.invalid"),
        (3, b"Human Maintainer"),
        (4, b"human@example.invalid"),
    ],
)
def test_nonmatching_metadata_is_checked_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: int, value: bytes
) -> None:
    repository, base, head = _repository(tmp_path, head_message="bad header")
    targets = read_range(repository, f"{base}..{head}", max_commits=2)
    fields = [
        head.encode(),
        b"dependabot[bot]",
        BOT_EMAIL.encode(),
        b"GitHub",
        b"noreply@github.com",
    ]
    fields[field] = value
    output = b"\0".join(fields) + b"\0"
    monkeypatch.setattr(
        dependabot,
        "run_git",
        lambda *args, **kwargs: ProcessResult(0, output, b"", False, False, False),
    )

    assert (
        dependabot.filter_dependabot_commits(
            repository, base_sha=base, head_sha=head, targets=targets
        )
        == frozenset()
    )


@pytest.mark.parametrize(
    "malformation",
    ["missing", "duplicate", "extra", "wrong-sha", "unterminated", "invalid-utf8", "oversized"],
)
def test_incomplete_or_malformed_metadata_never_produces_an_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformation: str
) -> None:
    repository, base, head = _repository(tmp_path, head_message="bad header")
    targets = read_range(repository, f"{base}..{head}", max_commits=2)
    fields = [
        head.encode(),
        b"dependabot[bot]",
        BOT_EMAIL.encode(),
        b"GitHub",
        b"noreply@github.com",
    ]
    if malformation == "wrong-sha":
        fields[0] = b"a" * 40
    if malformation == "invalid-utf8":
        fields[1] = b"\xff"
    if malformation == "oversized":
        fields[1] = b"x" * (dependabot.MAX_AUTHOR_FIELD_BYTES + 1)
    output = b"\0".join(fields) + b"\0"
    if malformation == "missing":
        output = b""
    if malformation == "duplicate":
        output += output
    if malformation == "extra":
        output += b"\0".join([base.encode(), *fields[1:]]) + b"\0"
    if malformation == "unterminated":
        output = output[:-1]
    monkeypatch.setattr(
        dependabot,
        "run_git",
        lambda *args, **kwargs: ProcessResult(0, output, b"", False, False, False),
    )

    with pytest.raises(GitError):
        dependabot.filter_dependabot_commits(
            repository, base_sha=base, head_sha=head, targets=targets
        )
