"""Create disposable positive/negative repositories for hosted composite Action controls."""

from __future__ import annotations

import json
import os
import shlex
import shutil
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
        "typos-valid",
        "typos-message",
        "typos-title",
        "typos-missing",
        "typos-broken",
        "typos-bot",
        "typos-bot-lookalike",
        "typos-bot-fork",
        "typos-bot-branch",
        "typos-bot-stale",
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
    if scenario.startswith("typos-"):
        with (repo / ".yaga.toml").open("a", encoding="utf-8") as policy:
            policy.write('typos = "check"\ndependabot-pull-requests = "skip"\n')
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
    if scenario == "typos-message":
        message += "\n\nFix teh spelling."
    git("commit", "--allow-empty", "-m", message)
    head = git("rev-parse", "HEAD")
    git(
        "update-ref",
        "refs/remotes/pull/17/head",
        base if scenario in {"stale", "typos-bot-stale"} else head,
    )
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
    if scenario == "typos-title":
        event["pull_request"]["title"] = "fix: validate teh consumer contract"
    if scenario.startswith("typos-bot"):
        event["pull_request"]["user"] = {"login": "dependabot[bot]", "id": 42, "type": "Bot"}
        event["pull_request"]["head"]["ref"] = "dependabot/example"
        event["pull_request"]["title"] = "fix: validate teh consumer contract"
    if scenario == "typos-bot-lookalike":
        event["pull_request"]["user"] = {"login": "octocat", "id": 1, "type": "User"}
    if scenario == "typos-bot-fork":
        event["pull_request"]["head"]["repo"] = {"id": 200, "full_name": "other/repository"}
    if scenario == "typos-bot-branch":
        event["pull_request"]["head"]["ref"] = "feature"
    # Ambient spelling settings must not weaken trusted checks.
    (repo / "_typos.toml").write_text("[default]\ncheck-file = false\n", encoding="utf-8")
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
        "GITHUB_HEAD_REF": event["pull_request"]["head"]["ref"],
    }
    action = Path(os.environ["GITHUB_WORKSPACE"]) / "actions/commit-check/action.yml"
    source = action.read_text(encoding="utf-8")
    boundary = "        set -euo pipefail\n"
    if source.count(boundary) != 1:
        raise ValueError("composite shell boundary changed")
    exports = "".join(
        f"        export {key}={shlex.quote(value)}\n" for key, value in context.items()
    )
    if scenario in {"typos-missing", "typos-bot"}:
        exports += (
            '        IFS=: read -ra control_paths <<< "$PATH"\n'
            "        control_path=''\n"
            '        for directory in "${control_paths[@]}"; do\n'
            '          if [[ ! -f "$directory/typos" && ! -f "$directory/typos.exe" ]]; then\n'
            '            control_path="${control_path:+$control_path:}$directory"\n'
            "          fi\n"
            "        done\n"
            '        export PATH="$control_path"\n'
        )
    if scenario == "typos-broken":
        tools = repo.parent / "broken-tools"
        tools.mkdir()
        if os.name == "nt":
            shutil.copyfile(sys.executable, tools / "typos.exe")
        else:
            tool = tools / "typos"
            tool.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
            tool.chmod(0o755)
        tools_path = shlex.quote(tools.as_posix())
        if os.name == "nt":
            tools_path = f'"$(cygpath -u {tools_path})"'
        exports += f'        export PATH={tools_path}:"$PATH"\n'
    action.write_text(source.replace(boundary, boundary + exports), encoding="utf-8")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for name, value in {"repo": repo, "event": event_path, "revision": policy_revision}.items():
            output.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
