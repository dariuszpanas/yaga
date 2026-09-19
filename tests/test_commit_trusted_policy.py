"""Trusted main policy must never come from the PR or working tree."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_commit_action import ROOT, _action_fixture, _commit, _git
from tests.test_commit_consumer_policy import HEADER, POLICY, message


@pytest.mark.parametrize(
    "scenario,code",
    [
        ("valid", 0),
        ("message", 1),
        ("edited-title", 1),
        ("dirty-policy", 0),
        ("pr-policy", 1),
        ("stale-head", 2),
        ("wrong-checkout", 2),
        ("wrong-ref", 2),
        ("wrong-event", 2),
        ("missing-head", 2),
        ("malformed-event", 2),
        ("missing-policy", 2),
        ("symlink-policy", 2),
        ("invalid-policy", 2),
        ("traversal", 2),
        ("shallow", 2),
        ("missing-base", 2),
        ("bot", 0),
        ("bot-fork", 1),
        ("bot-branch", 1),
        ("bot-lookalike", 1),
    ],
)
def test_trusted_action_boundaries(tmp_path: Path, scenario: str, code: int) -> None:
    repo, event_path, env = _action_fixture(tmp_path)
    # Establish policy on trusted main, then a PR commit that cannot weaken it.
    (repo / ".yaga.toml").write_text(
        POLICY + 'dependabot-pull-requests = "skip"\n', encoding="utf-8"
    )
    _git(repo, "add", ".yaga.toml")
    if scenario == "invalid-policy":
        (repo / ".yaga.toml").write_text("not valid TOML!", encoding="utf-8")
        _git(repo, "add", ".yaga.toml")
    if scenario == "symlink-policy":
        oid = _git(repo, "hash-object", ".yaga.toml")
        _git(repo, "update-index", "--cacheinfo", "120000", oid, ".yaga.toml")
    base = _commit(repo, "chore: establish trusted policy")
    if scenario == "pr-policy":
        (repo / ".yaga.toml").write_text("[commit]\n", encoding="utf-8")
        _git(repo, "add", ".yaga.toml")
    head = _commit(
        repo,
        "invalid"
        if scenario in {"message", "pr-policy"} or scenario.startswith("bot")
        else message(),
    )
    _git(repo, "update-ref", "refs/remotes/pull/17/head", head)
    _git(repo, "checkout", "--detach", base)
    event = json.loads(event_path.read_text())
    event["repository"]["default_branch"] = "main"
    event["pull_request"]["base"]["sha"] = base
    event["pull_request"]["head"]["sha"] = head
    event["pull_request"]["title"] = HEADER
    env.update(
        GITHUB_EVENT_NAME="pull_request_target",
        GITHUB_SHA=base,
        GITHUB_REF="refs/heads/main",
        YAGA_COMMIT_TRUSTED_CONFIG=".yaga.toml",
    )
    if scenario == "edited-title":
        event["action"] = "edited"
        event["pull_request"]["title"] = "fix: Capitalized description"
    if scenario == "dirty-policy":
        (repo / ".yaga.toml").write_text("invalid TOML!", encoding="utf-8")
    if scenario == "stale-head":
        _git(repo, "update-ref", "refs/remotes/pull/17/head", base)
    if scenario == "missing-head":
        _git(repo, "update-ref", "-d", "refs/remotes/pull/17/head")
    if scenario == "wrong-checkout":
        _git(repo, "checkout", "--detach", head)
    if scenario == "wrong-ref":
        env["GITHUB_REF"] = "refs/heads/untrusted"
    if scenario == "wrong-event":
        env["GITHUB_EVENT_NAME"] = "pull_request"
    if scenario == "missing-policy":
        env["YAGA_COMMIT_TRUSTED_CONFIG"] = "missing/.yaga.toml"
    if scenario == "traversal":
        env["YAGA_COMMIT_TRUSTED_CONFIG"] = "../.yaga.toml"
    if scenario == "malformed-event":
        event["number"] = True
    if scenario == "missing-base":
        event["pull_request"]["base"]["sha"] = "a" * 40
    if scenario == "shallow":
        (repo / ".git/shallow").write_text(base + "\n", encoding="ascii")
    if scenario.startswith("bot"):
        event["pull_request"]["user"] = {"login": "dependabot[bot]", "id": 49699333, "type": "Bot"}
        event["pull_request"]["head"]["ref"] = "dependabot/pip/update"
        env["GITHUB_HEAD_REF"] = "dependabot/pip/update"
        if scenario == "bot-fork":
            event["pull_request"]["head"]["repo"] = {"id": 200, "full_name": "fork/repository"}
        if scenario == "bot-branch":
            event["pull_request"]["head"]["ref"] = env["GITHUB_HEAD_REF"] = "feature"
        if scenario == "bot-lookalike":
            event["pull_request"]["user"]["login"] = "dependabot"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    environment = dict(
        os.environ, **env, PYTHONPATH=str(ROOT / "src"), YAGA_COMMIT_ACTION_RUNTIME="1"
    )
    environment.pop("YAGA_ACTION_RUNTIME", None)
    result = subprocess.run(
        [
            sys.executable,
            "-P",
            "-S",
            "-m",
            "yaga",
            "github",
            "pull-request",
            "check",
            "--event-file",
            str(event_path),
            "--repo",
            str(repo),
            "--format",
            "github",
        ],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == code, result.stdout + result.stderr
    if scenario == "bot":
        assert "Dependabot pull request" in result.stdout
