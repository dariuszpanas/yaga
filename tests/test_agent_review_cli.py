"""Tests for pull-request review CLI checks across offline snapshots and bounded gh fetches."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from yaga.agent_review import remote
from yaga.agent_review.plan import build_plan
from yaga.agent_review.policy import (
    CODEX_DEFAULT_BOT_APP_ID,
    CODEX_DEFAULT_BOT_LOGIN,
    CODEX_DEFAULT_BOT_USER_ID,
    COPILOT_DEFAULT_BOT_LOGIN,
    load_policy,
)
from yaga.agent_review.remote import (
    GH_TIMEOUT_SECONDS,
    MAX_GH_STDERR_BYTES,
    MAX_GH_STDOUT_BYTES,
    PullRequestReference,
    fetch_pull_request_review_snapshot,
    parse_pull_request_reference,
)
from yaga.cli import app
from yaga.errors import InputError
from yaga.git.runtime import ProcessResult

ROOT = Path(__file__).parents[1]
runner = CliRunner()
COMMAND = ["pull-request", "review", "check"]
HEAD_SHA = "a" * 40
OLD_SHA = "b" * 40
BASE_SHA = "c" * 40
HEAD_TIME = "2026-11-01T18:00:00Z"
REFERENCE = PullRequestReference("github.com", "example", "project", 23)


def _write_policy(tmp_path: Path) -> Path:
    policy_path = tmp_path / "agent-review-policy.toml"
    policy_path.write_text(
        """
[agent-review]
version = 1
required = ["codex", "copilot"]
aggregation = "all-required"

[agent-review.agents.codex]
preset = "codex"
outcome = "review"
publication = "review"

[agent-review.agents.copilot]
preset = "copilot"
outcome = "review"
publication = "review"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return policy_path


def _codex_summary_body(
    commit_sha: str = HEAD_SHA,
    *,
    status: str = "completed",
    has_any_findings: bool | None = False,
    has_unresolved_findings: bool | None = False,
) -> str:
    marker = json.dumps(
        {
            "code_review": {
                "commit_sha": commit_sha,
                "has_any_findings": has_any_findings,
                "has_unresolved_findings": has_unresolved_findings,
                "status": status,
            },
            "security_review": {
                "commit_sha": None,
                "has_any_findings": None,
                "has_unresolved_findings": None,
                "status": None,
            },
        },
        separators=(",", ":"),
    )
    return f"<!-- codex-pull-request-review-summary -->\n<!-- codex-review-state:v1 {marker} -->"


def _snapshot_payload(
    policy_path: Path,
    *,
    head_committed_at: str | None = HEAD_TIME,
    reviews: list[dict[str, Any]] | None = None,
    review_comments: list[dict[str, Any]] | None = None,
    issue_comments: list[dict[str, Any]] | None = None,
    reactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    policy = load_policy(policy_path)
    return {
        "version": 1,
        "plan_digest": build_plan(policy).digest(),
        "number": 23,
        "url": REFERENCE.url,
        "state": "open",
        "draft": False,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "head_committed_at": head_committed_at,
        "reviews": reviews or [],
        "review_comments": review_comments or [],
        "issue_comments": issue_comments or [],
        "reactions": reactions or [],
    }


def _default_gh_endpoints(
    *,
    pr_overrides: dict[str, Any] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    review_comments: list[dict[str, Any]] | None = None,
    issue_comments: list[dict[str, Any]] | None = None,
    reactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    pr_doc: dict[str, Any] = {
        "number": 23,
        "html_url": REFERENCE.url,
        "state": "open",
        "draft": False,
        "head": {"sha": HEAD_SHA.upper()},
        "base": {"sha": BASE_SHA},
    }
    if pr_overrides:
        pr_doc.update(pr_overrides)
    base = "repos/example/project"
    return {
        f"{base}/pulls/23": pr_doc,
        f"{base}/pulls/23/reviews?per_page=100": (
            reviews
            if reviews is not None
            else [
                {
                    "id": 10,
                    "user": {"login": COPILOT_DEFAULT_BOT_LOGIN, "id": 9001},
                    "state": "COMMENTED",
                    "commit_id": HEAD_SHA,
                    "submitted_at": "2026-11-01T18:03:00Z",
                    "body": "Copilot reviewed 2 files and generated no comments.",
                }
            ]
        ),
        f"{base}/pulls/23/comments?per_page=100": review_comments or [],
        f"{base}/issues/23/comments?per_page=100": (
            issue_comments
            if issue_comments is not None
            else [
                {
                    "id": 15,
                    "user": {
                        "login": CODEX_DEFAULT_BOT_LOGIN,
                        "id": CODEX_DEFAULT_BOT_USER_ID,
                    },
                    "performed_via_github_app": {"id": CODEX_DEFAULT_BOT_APP_ID},
                    "created_at": "2026-11-01T18:01:00Z",
                    "updated_at": "2026-11-01T18:04:00Z",
                    "body": _codex_summary_body(HEAD_SHA),
                }
            ]
        ),
        f"{base}/issues/23/reactions?per_page=100": (
            reactions
            if reactions is not None
            else [
                {
                    "id": 20,
                    "user": {
                        "login": CODEX_DEFAULT_BOT_LOGIN,
                        "id": CODEX_DEFAULT_BOT_USER_ID,
                    },
                    "content": "+1",
                    "created_at": "2026-11-01T18:04:00Z",
                }
            ]
        ),
    }


def _mock_gh_api(
    monkeypatch: pytest.MonkeyPatch,
    endpoints: dict[str, Any],
) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(remote.shutil, "which", lambda _name: sys.executable)

    def run(command: list[str], **options: Any) -> ProcessResult:
        calls.append((command, options))
        endpoint = command[-1]
        if endpoint not in endpoints:
            return ProcessResult(1, b"", b"not found", False, False, False)
        value = endpoints[endpoint]
        if isinstance(value, ProcessResult):
            return value
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        return ProcessResult(0, payload, b"", False, False, False)

    monkeypatch.setattr(remote, "run_bounded_process", run)
    return calls


def test_repository_agent_review_policy_is_valid() -> None:
    policy = load_policy(ROOT / ".yaga" / "agent-review-policy.toml")
    assert policy.required == ("codex",)
    assert policy.lenses[0].resolve_bot().bot_login == CODEX_DEFAULT_BOT_LOGIN


@pytest.mark.parametrize(
    ("value", "repository", "expected"),
    [
        ("23", "example/project", REFERENCE),
        ("23", "github.com/example/project", REFERENCE),
        (
            "23",
            "GIT.EXAMPLE.COM/example/project",
            PullRequestReference("git.example.com", "example", "project", 23),
        ),
        (REFERENCE.url, None, REFERENCE),
        (REFERENCE.url + "/", None, REFERENCE),
    ],
)
def test_parse_pull_request_reference_accepts_explicit_selectors(
    value: str,
    repository: str | None,
    expected: PullRequestReference,
) -> None:
    selected = parse_pull_request_reference(value, repository)
    assert selected == expected
    assert selected.gh_repository == f"{expected.host}/{expected.owner}/{expected.repository}"


@pytest.mark.parametrize(
    ("value", "repository"),
    [
        ("23", None),
        ("0", "example/project"),
        ("023", "example/project"),
        ("-23", "example/project"),
        (str(2**63), "example/project"),
        (REFERENCE.url, "example/project"),
        ("http://github.com/example/project/pull/23", None),
        ("https://user@github.com/example/project/pull/23", None),
        ("https://github.com:443/example/project/pull/23", None),
        ("https://github.com/example/project/pull/23?x=1", None),
        ("https://github.com/example/project/pull/23#frag", None),
        ("https://github.com/example/project/pull/%32%33", None),
    ],
)
def test_parse_pull_request_reference_rejects_ambiguous_or_unsafe_selectors(
    value: str,
    repository: str | None,
) -> None:
    with pytest.raises(InputError):
        parse_pull_request_reference(value, repository)


def test_fetch_pull_request_review_snapshot_executes_bounded_gh_api_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = _write_policy(tmp_path)
    plan_digest = build_plan(load_policy(policy_path)).digest()
    monkeypatch.setenv("GH_HOST", "evil.example.com")
    monkeypatch.setenv("GH_REPO", "evil/repo")
    monkeypatch.setenv("GH_DEBUG", "api")
    calls = _mock_gh_api(monkeypatch, _default_gh_endpoints())

    snapshot = fetch_pull_request_review_snapshot(REFERENCE, plan_digest=plan_digest)

    assert snapshot.number == 23
    assert snapshot.head_sha == HEAD_SHA
    assert snapshot.base_sha == BASE_SHA
    assert snapshot.head_committed_at is None
    assert snapshot.plan_digest == plan_digest
    assert len(calls) == 5
    for command, options in calls:
        assert command[:3] == [str(Path(sys.executable).resolve()), "api", "--hostname"]
        assert command[3] == "github.com"
        assert options["stdout_limit"] == MAX_GH_STDOUT_BYTES
        assert options["stderr_limit"] == MAX_GH_STDERR_BYTES
        assert options["timeout_seconds"] == GH_TIMEOUT_SECONDS
        env = options["environment"]
        assert "GH_HOST" not in env
        assert "GH_REPO" not in env
        assert "GH_DEBUG" not in env
        assert env["GH_PROMPT_DISABLED"] == "1"


def test_live_fetch_requires_head_bound_evidence_and_rejects_unbound_reaction_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = _write_policy(tmp_path)
    # Without a commit-bound review or summary on HEAD_SHA, an unbound +1
    # reaction alone remains pending on live --pr fetches.
    _mock_gh_api(monkeypatch, _default_gh_endpoints(issue_comments=[]))
    res_reaction_only = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), "--pr", REFERENCE.url, "--format", "json"],
    )
    assert res_reaction_only.exit_code == 1
    assert json.loads(res_reaction_only.stdout)["status"] == "pending"

    # With a head-bound Codex summary (or review) for HEAD_SHA, live fetch passes.
    _mock_gh_api(monkeypatch, _default_gh_endpoints())
    res_head_bound = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), "--pr", REFERENCE.url, "--format", "json"],
    )
    assert res_head_bound.exit_code == 0, res_head_bound.stderr
    assert json.loads(res_head_bound.stdout)["status"] == "passed"


@pytest.mark.parametrize(
    ("endpoint_key", "bad_value", "match"),
    [
        (
            "repos/example/project/pulls/23",
            ProcessResult(0, b"{}", b"", False, False, True),
            "30-second",
        ),
        (
            "repos/example/project/pulls/23",
            ProcessResult(0, b"{}", b"", True, False, False),
            "output limit",
        ),
        (
            "repos/example/project/pulls/23",
            ProcessResult(1, b"", b"auth failed", False, False, False),
            "could not fetch",
        ),
        (
            "repos/example/project/pulls/23",
            {
                "number": 99,
                "html_url": REFERENCE.url,
                "state": "open",
                "head": {"sha": HEAD_SHA},
                "base": {"sha": BASE_SHA},
            },
            "pull-request number",
        ),
        (
            "repos/example/project/pulls/23",
            {
                "number": 23,
                "html_url": "https://github.com/other/project/pull/23",
                "state": "open",
                "head": {"sha": HEAD_SHA},
                "base": {"sha": BASE_SHA},
            },
            "pull-request identity",
        ),
        (
            "repos/example/project/issues/23/comments?per_page=100",
            [
                {
                    "id": 1,
                    "user": {
                        "login": CODEX_DEFAULT_BOT_LOGIN,
                        "id": CODEX_DEFAULT_BOT_USER_ID,
                    },
                    "performed_via_github_app": "not-an-object",
                    "created_at": "2026-11-01T18:02:00Z",
                    "body": "Codex Review: Didn't find any major issues.",
                }
            ],
            "performed_via_github_app",
        ),
    ],
)
def test_fetch_pull_request_review_snapshot_rejects_invalid_remote_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint_key: str,
    bad_value: Any,
    match: str,
) -> None:
    policy_path = _write_policy(tmp_path)
    plan_digest = build_plan(load_policy(policy_path)).digest()
    endpoints = _default_gh_endpoints()
    endpoints[endpoint_key] = bad_value
    _mock_gh_api(monkeypatch, endpoints)

    with pytest.raises(InputError, match=match):
        fetch_pull_request_review_snapshot(REFERENCE, plan_digest=plan_digest)


def test_offline_and_remote_review_checks_match_across_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = _write_policy(tmp_path)
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(
        json.dumps(
            _snapshot_payload(
                policy_path,
                head_committed_at=None,
                reviews=[
                    {
                        "id": 10,
                        "author_login": COPILOT_DEFAULT_BOT_LOGIN,
                        "author_id": 9001,
                        "state": "COMMENTED",
                        "commit_id": HEAD_SHA,
                        "submitted_at": "2026-11-01T18:03:00Z",
                        "body": "Copilot reviewed 2 files and generated no comments.",
                    }
                ],
                issue_comments=[
                    {
                        "id": 15,
                        "author_login": CODEX_DEFAULT_BOT_LOGIN,
                        "author_id": CODEX_DEFAULT_BOT_USER_ID,
                        "app_id": CODEX_DEFAULT_BOT_APP_ID,
                        "created_at": "2026-11-01T18:01:00Z",
                        "updated_at": "2026-11-01T18:04:00Z",
                        "body": _codex_summary_body(HEAD_SHA),
                    }
                ],
                reactions=[
                    {
                        "id": 20,
                        "author_login": CODEX_DEFAULT_BOT_LOGIN,
                        "author_id": CODEX_DEFAULT_BOT_USER_ID,
                        "content": "+1",
                        "created_at": "2026-11-01T18:04:00Z",
                    }
                ],
            )
        ),
        encoding="utf-8",
    )
    _mock_gh_api(monkeypatch, _default_gh_endpoints())

    local_json = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), "--file", str(snapshot_file), "--format", "json"],
    )
    remote_json = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), "--pr", REFERENCE.url, "--format", "json"],
    )

    assert local_json.exit_code == 0, local_json.stderr
    assert remote_json.exit_code == 0, remote_json.stderr
    local_doc = json.loads(local_json.stdout)
    remote_doc = json.loads(remote_json.stdout)
    assert local_doc["schema_version"] == 1
    assert local_doc["kind"] == "pull_request_review"
    assert local_doc["status"] == "passed"
    assert local_doc["valid"] is True
    for key in (
        "status",
        "valid",
        "pull_request_number",
        "head_sha",
        "base_sha",
        "plan_digest",
        "results",
    ):
        assert local_doc[key] == remote_doc[key]

    text_res = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), "--file", str(snapshot_file)],
    )
    assert text_res.exit_code == 0
    assert "Pull request: #23" in text_res.stdout
    assert "Agent review result: passed." in text_res.stdout

    github_res = runner.invoke(
        app,
        [
            *COMMAND,
            "--policy",
            str(policy_path),
            "--file",
            str(snapshot_file),
            "--format",
            "github",
        ],
    )
    assert github_res.exit_code == 0
    assert "::notice title=YAGA Agent review::" in github_res.stdout

    quiet_res = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), "--file", str(snapshot_file), "--quiet"],
    )
    assert quiet_res.exit_code == 0
    assert quiet_res.stdout == ""


def test_review_check_exits_one_when_pending_or_failed(tmp_path: Path) -> None:
    policy_path = _write_policy(tmp_path)
    pending_file = tmp_path / "pending.json"
    pending_file.write_text(
        json.dumps(
            _snapshot_payload(
                policy_path,
                reactions=[
                    {
                        "id": 30,
                        "author_login": CODEX_DEFAULT_BOT_LOGIN,
                        "author_id": CODEX_DEFAULT_BOT_USER_ID,
                        "content": "eyes",
                        "created_at": "2026-11-01T18:02:00Z",
                    }
                ],
            )
        ),
        encoding="utf-8",
    )
    pending_res = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), "--file", str(pending_file), "--format", "json"],
    )
    assert pending_res.exit_code == 1
    pending_doc = json.loads(pending_res.stdout)
    assert pending_doc["status"] == "pending"
    assert pending_doc["valid"] is False

    failed_file = tmp_path / "failed.json"
    failed_file.write_text(
        json.dumps(
            _snapshot_payload(
                policy_path,
                issue_comments=[
                    {
                        "id": 40,
                        "author_login": CODEX_DEFAULT_BOT_LOGIN,
                        "author_id": CODEX_DEFAULT_BOT_USER_ID,
                        "app_id": CODEX_DEFAULT_BOT_APP_ID,
                        "created_at": "2026-11-01T18:03:00Z",
                        "body": "Unable to review this pull request due to usage limits.",
                    }
                ],
            )
        ),
        encoding="utf-8",
    )
    failed_res = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), "--file", str(failed_file), "--format", "github"],
    )
    assert failed_res.exit_code == 1
    assert "::error title=YAGA Agent review%3A codex::" in failed_res.stdout


@pytest.mark.parametrize(
    ("extra_args", "output_format"),
    [
        ([], "text"),
        (["--file", "missing.json", "--pr", REFERENCE.url], "json"),
        (["--file", "missing.json", "--repo", "example/project"], "github"),
        (["--pr", "23"], "json"),
        (["--pr", REFERENCE.url, "--repo", "example/project"], "text"),
    ],
)
def test_review_check_exits_two_on_input_and_selection_errors(
    tmp_path: Path,
    extra_args: list[str],
    output_format: str,
) -> None:
    policy_path = _write_policy(tmp_path)
    result = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), *extra_args, "--format", output_format],
    )
    assert result.exit_code == 2
    assert result.stdout == ""
    if output_format == "json":
        err_doc = json.loads(result.stderr)
        assert err_doc["schema_version"] == 1
        assert err_doc["kind"] == "pull_request_review"
        assert err_doc["status"] == "error"
        assert err_doc["valid"] is False
    elif output_format == "github":
        assert result.stderr.startswith("::error title=YAGA PR review::")
    else:
        assert "YAGA input error:" in result.stderr


@pytest.mark.parametrize("module_name", ["yaga.action_cli", "yaga.commit_action_cli"])
def test_action_bootstrap_does_not_import_pull_request_review_cli_modules(
    module_name: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            *(("-S",) if module_name == "yaga.action_cli" else ()),
            "-c",
            (
                "import json, sys; "
                f"sys.path.insert(0, {str(ROOT / 'src')!r}); "
                f"import {module_name}; "
                "print(json.dumps({"
                "'remote': 'yaga.agent_review.remote' in sys.modules, "
                "'service': 'yaga.agent_review.service' in sys.modules, "
                "'pull_request_cmd': 'yaga.commands.pull_request' in sys.modules"
                "}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "remote": False,
        "service": False,
        "pull_request_cmd": False,
    }


def test_live_pr_fetch_honors_delay_seconds_and_check_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "claude-policy.toml"
    policy_path.write_text(
        """
[agent-review]
version = 1
required = ["claude"]

[agent-review.agents.claude]
check-runs = ["claude-review"]
delay-seconds = 12
""".strip()
        + "\n",
        encoding="utf-8",
    )
    slept: list[int] = []
    monkeypatch.setattr(remote.time, "sleep", lambda seconds: slept.append(seconds))

    endpoints = _default_gh_endpoints(reviews=[], issue_comments=[], reactions=[])
    check_runs_endpoint = f"repos/example/project/commits/{HEAD_SHA}/check-runs?per_page=100"
    endpoints[check_runs_endpoint] = {
        "total_count": 1,
        "check_runs": [
            {
                "id": 900,
                "name": "claude-review",
                "status": "completed",
                "conclusion": "success",
                "head_sha": HEAD_SHA,
                "started_at": "2026-11-01T18:01:00Z",
                "completed_at": "2026-11-01T18:02:00Z",
                "app": {"id": 555, "slug": "claude"},
            }
        ],
    }
    calls = _mock_gh_api(monkeypatch, endpoints)

    res = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), "--pr", REFERENCE.url, "--format", "json"],
    )
    assert res.exit_code == 0, res.stderr
    assert slept == [12]
    assert len(calls) == 6
    doc = json.loads(res.stdout)
    assert doc["status"] == "passed"
    assert doc["results"] == [
        {
            "lens": "claude",
            "outcome": "passed",
            "summary": f"check-run claude-review for claude[bot] passed on head {HEAD_SHA[:12]}",
        }
    ]

    endpoints[check_runs_endpoint] = {
        "total_count": 100,
        "check_runs": [],
    }
    _mock_gh_api(monkeypatch, endpoints)
    truncated_res = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), "--pr", REFERENCE.url, "--format", "json"],
    )
    assert truncated_res.exit_code == 2


def test_wait_seconds_polls_while_actively_in_progress_and_stops_when_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "codex-policy.toml"
    policy_path.write_text(
        """
[agent-review]
version = 1
required = ["codex"]

[agent-review.agents.codex]
wait-seconds = 60
""".strip()
        + "\n",
        encoding="utf-8",
    )
    slept: list[int] = []
    monkeypatch.setattr(remote.time, "sleep", lambda seconds: slept.append(seconds))

    running_comments = [
        {
            "id": 15,
            "user": {
                "login": CODEX_DEFAULT_BOT_LOGIN,
                "id": CODEX_DEFAULT_BOT_USER_ID,
            },
            "performed_via_github_app": {"id": CODEX_DEFAULT_BOT_APP_ID},
            "created_at": "2026-11-01T18:01:00Z",
            "updated_at": "2026-11-01T18:02:00Z",
            "body": _codex_summary_body(
                HEAD_SHA,
                status="running",
                has_any_findings=None,
                has_unresolved_findings=None,
            ),
        }
    ]
    completed_comments = [
        {
            "id": 15,
            "user": {
                "login": CODEX_DEFAULT_BOT_LOGIN,
                "id": CODEX_DEFAULT_BOT_USER_ID,
            },
            "performed_via_github_app": {"id": CODEX_DEFAULT_BOT_APP_ID},
            "created_at": "2026-11-01T18:01:00Z",
            "updated_at": "2026-11-01T18:04:00Z",
            "body": _codex_summary_body(HEAD_SHA),
        }
    ]
    endpoints = _default_gh_endpoints(reviews=[], issue_comments=running_comments, reactions=[])
    fetch_count = 0
    monkeypatch.setattr(remote.shutil, "which", lambda _name: sys.executable)

    def run(command: list[str], **options: Any) -> ProcessResult:
        nonlocal fetch_count
        endpoint = command[-1]
        if endpoint == "repos/example/project/issues/23/comments?per_page=100":
            fetch_count += 1
            comments = running_comments if fetch_count < 3 else completed_comments
            return ProcessResult(0, json.dumps(comments).encode("utf-8"), b"", False, False, False)
        return ProcessResult(
            0, json.dumps(endpoints[endpoint]).encode("utf-8"), b"", False, False, False
        )

    monkeypatch.setattr(remote, "run_bounded_process", run)

    res = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), "--pr", REFERENCE.url, "--format", "json"],
    )
    assert res.exit_code == 0, res.stderr
    assert slept == [15, 15]
    assert json.loads(res.stdout)["status"] == "passed"


def test_wait_seconds_stops_after_startup_grace_when_no_review_is_actively_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "codex-policy.toml"
    policy_path.write_text(
        """
[agent-review]
version = 1
required = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    slept: list[int] = []
    monkeypatch.setattr(remote.time, "sleep", lambda seconds: slept.append(seconds))

    stale_comments = [
        {
            "id": 15,
            "user": {
                "login": CODEX_DEFAULT_BOT_LOGIN,
                "id": CODEX_DEFAULT_BOT_USER_ID,
            },
            "performed_via_github_app": {"id": CODEX_DEFAULT_BOT_APP_ID},
            "created_at": "2026-11-01T17:50:00Z",
            "updated_at": "2026-11-01T17:52:00Z",
            "body": _codex_summary_body(OLD_SHA),
        }
    ]
    _mock_gh_api(
        monkeypatch,
        _default_gh_endpoints(reviews=[], issue_comments=stale_comments, reactions=[]),
    )

    res = runner.invoke(
        app,
        [
            *COMMAND,
            "--policy",
            str(policy_path),
            "--pr",
            REFERENCE.url,
            "--wait-seconds",
            "300",
            "--format",
            "json",
        ],
    )
    assert res.exit_code == 1
    # Stops after the 30-second startup grace (two 15s polls) instead of waiting 300s.
    assert slept == [15, 15]
    assert json.loads(res.stdout)["status"] == "pending"


def test_submit_review_posts_idempotent_approve_and_request_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "codex-policy.toml"
    policy_path.write_text(
        """
[agent-review]
version = 1
required = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    endpoints = _default_gh_endpoints(reviews=[], reactions=[])
    endpoints["repos/example/project/pulls/23/reviews"] = {
        "id": 501,
        "state": "APPROVED",
        "commit_id": HEAD_SHA,
    }
    calls = _mock_gh_api(monkeypatch, endpoints)

    approve_res = runner.invoke(
        app,
        [
            *COMMAND,
            "--policy",
            str(policy_path),
            "--pr",
            REFERENCE.url,
            "--submit-review",
            "approve",
            "--format",
            "json",
        ],
    )
    assert approve_res.exit_code == 0, approve_res.stderr
    approve_doc = json.loads(approve_res.stdout)
    assert approve_doc["submitted_review"] == {
        "action": "submitted",
        "event": "APPROVE",
        "state": "APPROVED",
        "review_id": 501,
        "commit_id": HEAD_SHA,
    }
    post_cmd, _ = calls[-1]
    assert "--method" in post_cmd and "POST" in post_cmd
    assert f"commit_id={HEAD_SHA}" in post_cmd
    assert "event=APPROVE" in post_cmd

    # Re-running when the same actor already has the latest APPROVED YAGA review on HEAD_SHA skips POST.
    existing_yaga_review = {
        "id": 501,
        "user": {"login": "dariuszpanas", "id": 15094983},
        "state": "APPROVED",
        "commit_id": HEAD_SHA,
        "submitted_at": "2026-11-01T18:05:00Z",
        "body": (
            f"YAGA pull-request review check passed for commit `{HEAD_SHA[:12]}`.\n\n"
            "<!-- yaga-pull-request-review:v1 -->"
        ),
    }
    endpoints = _default_gh_endpoints(reviews=[existing_yaga_review], reactions=[])
    endpoints["user"] = {"login": "dariuszpanas", "id": 15094983}
    calls = _mock_gh_api(monkeypatch, endpoints)

    unchanged_res = runner.invoke(
        app,
        [
            *COMMAND,
            "--policy",
            str(policy_path),
            "--pr",
            REFERENCE.url,
            "--submit-review",
            "both",
            "--format",
            "json",
        ],
    )
    assert unchanged_res.exit_code == 0, unchanged_res.stderr
    unchanged_doc = json.loads(unchanged_res.stdout)
    assert unchanged_doc["submitted_review"] == {
        "action": "unchanged",
        "event": "APPROVE",
        "state": "APPROVED",
        "review_id": 501,
        "commit_id": HEAD_SHA,
    }
    assert all("POST" not in cmd for cmd, _ in calls)

    # Failed evaluation with --submit-review request-changes submits REQUEST_CHANGES and exits 1.
    failed_comments = [
        {
            "id": 15,
            "user": {
                "login": CODEX_DEFAULT_BOT_LOGIN,
                "id": CODEX_DEFAULT_BOT_USER_ID,
            },
            "performed_via_github_app": {"id": CODEX_DEFAULT_BOT_APP_ID},
            "created_at": "2026-11-01T18:01:00Z",
            "updated_at": "2026-11-01T18:04:00Z",
            "body": _codex_summary_body(
                HEAD_SHA,
                status="completed",
                has_any_findings=True,
                has_unresolved_findings=True,
            ),
        }
    ]
    endpoints = _default_gh_endpoints(reviews=[], issue_comments=failed_comments, reactions=[])
    endpoints["repos/example/project/pulls/23/reviews"] = {
        "id": 502,
        "state": "CHANGES_REQUESTED",
        "commit_id": HEAD_SHA,
    }
    calls = _mock_gh_api(monkeypatch, endpoints)

    req_changes_res = runner.invoke(
        app,
        [
            *COMMAND,
            "--policy",
            str(policy_path),
            "--pr",
            REFERENCE.url,
            "--submit-review",
            "request-changes",
            "--format",
            "json",
        ],
    )
    assert req_changes_res.exit_code == 1
    req_changes_doc = json.loads(req_changes_res.stdout)
    assert req_changes_doc["status"] == "failed"
    assert req_changes_doc["submitted_review"] == {
        "action": "submitted",
        "event": "REQUEST_CHANGES",
        "state": "CHANGES_REQUESTED",
        "review_id": 502,
        "commit_id": HEAD_SHA,
    }
    post_cmd, _ = calls[-1]
    assert "event=REQUEST_CHANGES" in post_cmd


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--file", "snap.json", "--wait-seconds", "10"],
        ["--pr", REFERENCE.url, "--wait-seconds", "-1"],
        ["--pr", REFERENCE.url, "--wait-seconds", "601"],
        ["--file", "snap.json", "--submit-review", "approve"],
    ],
)
def test_wait_and_submit_review_reject_invalid_options(
    tmp_path: Path,
    extra_args: list[str],
) -> None:
    policy_path = _write_policy(tmp_path)
    res = runner.invoke(
        app,
        [*COMMAND, "--policy", str(policy_path), *extra_args, "--format", "json"],
    )
    assert res.exit_code == 2


def test_wait_seconds_counts_fetch_duration_toward_wall_clock_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "codex-policy.toml"
    policy_path.write_text(
        """
[agent-review]
version = 1
required = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    clock = 100.0
    slept: list[int | float] = []

    def fake_monotonic() -> float:
        return clock

    def fake_sleep(seconds: int | float) -> None:
        nonlocal clock
        slept.append(seconds)
        clock += seconds

    monkeypatch.setattr(remote.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(remote.time, "sleep", fake_sleep)

    running_comments = [
        {
            "id": 15,
            "user": {
                "login": CODEX_DEFAULT_BOT_LOGIN,
                "id": CODEX_DEFAULT_BOT_USER_ID,
            },
            "performed_via_github_app": {"id": CODEX_DEFAULT_BOT_APP_ID},
            "created_at": "2026-11-01T18:01:00Z",
            "updated_at": "2026-11-01T18:02:00Z",
            "body": _codex_summary_body(
                HEAD_SHA,
                status="running",
                has_any_findings=None,
                has_unresolved_findings=None,
            ),
        }
    ]
    endpoints = _default_gh_endpoints(reviews=[], issue_comments=running_comments, reactions=[])
    monkeypatch.setattr(remote.shutil, "which", lambda _name: sys.executable)

    def run(command: list[str], **options: Any) -> ProcessResult:
        nonlocal clock
        endpoint = command[-1]
        if endpoint == "repos/example/project/issues/23/comments?per_page=100":
            clock += 20.0
        return ProcessResult(
            0, json.dumps(endpoints[endpoint]).encode("utf-8"), b"", False, False, False
        )

    monkeypatch.setattr(remote, "run_bounded_process", run)

    res = runner.invoke(
        app,
        [
            *COMMAND,
            "--policy",
            str(policy_path),
            "--pr",
            REFERENCE.url,
            "--wait-seconds",
            "60",
            "--format",
            "json",
        ],
    )
    assert res.exit_code == 1
    # Each poll cycle takes 15s sleep + 20s fetch = 35s, so 2 cycles (70s) reach the 60s deadline.
    assert slept == [15, 15]

    # A sub-50ms remaining budget (e.g. 15s sleep + 14.98s fetch against --wait-seconds 30)
    # preserves the positive fractional sleep rather than rounding to 0.
    slept.clear()
    clock = 100.0

    def run_sub_50ms(command: list[str], **options: Any) -> ProcessResult:
        nonlocal clock
        endpoint = command[-1]
        if endpoint == "repos/example/project/issues/23/comments?per_page=100":
            clock += 14.98
        elif endpoint == "repos/example/project/pulls/23/comments?per_page=100":
            # GitHub REST API may re-anchor commit_id to HEAD_SHA while keeping original_commit_id on OLD_SHA.
            reanchored = [
                {
                    "id": 99,
                    "pull_request_review_id": 11,
                    "user": {
                        "login": CODEX_DEFAULT_BOT_LOGIN,
                        "id": CODEX_DEFAULT_BOT_USER_ID,
                    },
                    "commit_id": HEAD_SHA,
                    "original_commit_id": OLD_SHA,
                    "created_at": "2026-11-01T17:50:00Z",
                    "path": "src/app.py",
                    "body": "Stale inline finding re-anchored by GitHub diff tracker.",
                }
            ]
            return ProcessResult(
                0, json.dumps(reanchored).encode("utf-8"), b"", False, False, False
            )
        return ProcessResult(
            0, json.dumps(endpoints[endpoint]).encode("utf-8"), b"", False, False, False
        )

    monkeypatch.setattr(remote, "run_bounded_process", run_sub_50ms)
    sub_res = runner.invoke(
        app,
        [
            *COMMAND,
            "--policy",
            str(policy_path),
            "--pr",
            REFERENCE.url,
            "--wait-seconds",
            "30",
            "--format",
            "json",
        ],
    )
    assert sub_res.exit_code == 1
    assert len(slept) == 2
    assert slept[0] == 15
    assert 0 < slept[1] < 0.05
    assert json.loads(sub_res.stdout)["status"] == "pending"


def test_submit_review_resubmits_when_later_unmarked_review_overrode_marked_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "codex-policy.toml"
    policy_path.write_text(
        """
[agent-review]
version = 1
required = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    existing_reviews = [
        {
            "id": 501,
            "user": {"login": "github-actions[bot]", "id": 41898282},
            "state": "APPROVED",
            "commit_id": HEAD_SHA,
            "submitted_at": "2026-11-01T18:05:00Z",
            "body": "YAGA pull-request review check passed.\n\n<!-- yaga-pull-request-review:v1 -->",
        },
        {
            "id": 502,
            "user": {"login": "github-actions[bot]", "id": 41898282},
            "state": "CHANGES_REQUESTED",
            "commit_id": HEAD_SHA,
            "submitted_at": "2026-11-01T18:06:00Z",
            "body": "Manual follow-up blocking review without YAGA marker.",
        },
    ]
    endpoints = _default_gh_endpoints(reviews=existing_reviews, reactions=[])
    endpoints["user"] = {"login": "github-actions[bot]", "id": 41898282}
    endpoints["repos/example/project/pulls/23/reviews"] = {
        "id": 503,
        "state": "APPROVED",
        "commit_id": HEAD_SHA,
    }
    calls = _mock_gh_api(monkeypatch, endpoints)

    res = runner.invoke(
        app,
        [
            *COMMAND,
            "--policy",
            str(policy_path),
            "--pr",
            REFERENCE.url,
            "--submit-review",
            "approve",
            "--format",
            "json",
        ],
    )
    assert res.exit_code == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["submitted_review"] == {
        "action": "submitted",
        "event": "APPROVE",
        "state": "APPROVED",
        "review_id": 503,
        "commit_id": HEAD_SHA,
    }
    assert any("POST" in cmd for cmd, _ in calls)


def test_submit_review_skips_draft_pr_and_emits_github_notice_on_open_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "codex-policy.toml"
    policy_path.write_text(
        """
[agent-review]
version = 1
required = ["codex"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    draft_endpoints = _default_gh_endpoints(
        reviews=[],
        reactions=[],
        pr_overrides={"draft": True},
    )
    draft_calls = _mock_gh_api(monkeypatch, draft_endpoints)
    draft_res = runner.invoke(
        app,
        [
            *COMMAND,
            "--policy",
            str(policy_path),
            "--pr",
            REFERENCE.url,
            "--submit-review",
            "both",
            "--format",
            "json",
        ],
    )
    assert draft_res.exit_code == 0, draft_res.stderr
    assert "submitted_review" not in json.loads(draft_res.stdout)
    assert all("POST" not in cmd for cmd, _ in draft_calls)

    open_endpoints = _default_gh_endpoints(reviews=[], reactions=[])
    open_endpoints["repos/example/project/pulls/23/reviews"] = {
        "id": 777,
        "state": "APPROVED",
        "commit_id": HEAD_SHA,
    }
    _mock_gh_api(monkeypatch, open_endpoints)
    gh_res = runner.invoke(
        app,
        [
            *COMMAND,
            "--policy",
            str(policy_path),
            "--pr",
            REFERENCE.url,
            "--submit-review",
            "approve",
            "--format",
            "github",
        ],
    )
    assert gh_res.exit_code == 0, gh_res.stderr
    assert (
        f"::notice title=YAGA pull-request review::Submitted review APPROVED (submitted, id 777) on head {HEAD_SHA[:12]}."
        in gh_res.stdout
    )
