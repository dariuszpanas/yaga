"""Create disposable positive/negative repositories for hosted composite Action controls."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


def main() -> None:
    scenario = sys.argv[1]
    if scenario not in {
        "valid",
        "invalid",
        "stale",
        "nondefault",
        "nondefault-invalid",
        "advanced-main",
    }:
        raise ValueError("unknown control")
    repo = Path(os.environ["RUNNER_TEMP"]) / f"yaga-control-{scenario}"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "YAGA Controls")
    git("config", "user.email", "controls@example.invalid")
    (repo / ".yaga.toml").write_text(
        '[commit]\nbody-policy = "required"\nrequired-colon-footer-tokens = ["Validation"]\n',
        encoding="utf-8",
    )
    git("add", ".yaga.toml")
    git("commit", "-m", "chore: establish trusted policy")
    base = git("rev-parse", "HEAD")
    policy_revision = base
    base_ref = "main"
    if scenario.startswith("nondefault"):
        base_ref = "release/maintenance"
        git("checkout", "-b", base_ref)
        # The target branch has weaker policy than main. Only main is authority.
        (repo / ".yaga.toml").write_text("[commit]\n", encoding="utf-8")
        git("add", ".yaga.toml")
        git("commit", "-m", "chore: establish a non-default base")
        base = git("rev-parse", "HEAD")
    # The PR attempts to weaken policy in every control; it must not be loaded.
    (repo / ".yaga.toml").write_text("[commit]\n", encoding="utf-8")
    git("add", ".yaga.toml")
    message = "fix: validate the consumer contract"
    if scenario not in {"invalid", "nondefault-invalid"}:
        message += "\n\nExercise the actual dependency-free composite runtime.\n\nValidation: hosted control."
    git("commit", "--allow-empty", "-m", message)
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/pull/17/head", base if scenario == "stale" else head)
    git("checkout", "--detach", policy_revision)
    if scenario == "advanced-main":
        (repo / ".yaga.toml").write_text('[commit]\nbody-policy = "forbidden"\n', encoding="utf-8")
        git("add", ".yaga.toml")
        git("commit", "-m", "chore: advance main after the event")
        git("update-ref", "refs/heads/main", git("rev-parse", "HEAD"))
        git("checkout", "--detach", policy_revision)
    identity = {"id": 100, "full_name": "owner/repository", "default_branch": "main"}
    event = {
        "action": "edited",
        "number": 17,
        "repository": identity,
        "pull_request": {
            "number": 17,
            "title": "fix: validate the consumer contract",
            "state": "open",
            "draft": False,
            "user": {"login": "octocat", "id": 1, "type": "User"},
            "base": {"sha": base, "ref": base_ref, "repo": identity},
            "head": {"sha": head, "ref": "feature", "repo": identity},
        },
    }
    event_path = repo.parent / f"yaga-control-{scenario}.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    # GitHub reserves GITHUB_* runner environment values. Inject the synthetic
    # context in this disposable checkout's shell step, never in runtime code.
    # The production Action remains unchanged in Git and has no test bypass.
    context = {
        "GITHUB_WORKSPACE": str(repo),
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_EVENT_NAME": "pull_request_target",
        "GITHUB_REPOSITORY": "owner/repository",
        "GITHUB_REPOSITORY_ID": "100",
        "GITHUB_SHA": policy_revision,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_BASE_REF": base_ref,
        "GITHUB_HEAD_REF": "feature",
    }
    action = Path(os.environ["GITHUB_WORKSPACE"]) / "actions/commit-check/action.yml"
    source = action.read_text(encoding="utf-8")
    boundary = "        set -euo pipefail\n"
    if source.count(boundary) != 1:
        raise ValueError("composite shell boundary changed")
    exports = "".join(
        f"        export {key}={shlex.quote(value)}\n" for key, value in context.items()
    )
    action.write_text(source.replace(boundary, boundary + exports), encoding="utf-8")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for name, value in {"repo": repo, "event": event_path, "revision": policy_revision}.items():
            output.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
