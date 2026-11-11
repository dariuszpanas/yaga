"""Closed command dispatcher for installed-CLI modes of the root GitHub Action."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from yaga.errors import InputError, safe_error_text

MAX_ACTION_INPUT_BYTES = 65_536
MAX_ACTION_LIST_ITEMS = 256
MAX_CLI_ACTION_SECONDS = 1_080

CLI_ACTION_CHECKS = frozenset(
    {
        "branch",
        "change",
        "mode",
        "path",
        "policy",
        "pr-description",
        "pr-review",
        "pr-triage",
        "repo",
        "size",
        "tree",
        "workflow",
        "workflow-lint",
        "workflow-security",
    }
)

ALL_CLI_INPUT_NAMES = (
    "policy",
    "template",
    "file",
    "pr",
    "repo",
    "wait-seconds",
    "submit-review",
    "triage-policy",
    "publish-comment",
    "branch",
    "range",
    "revision",
    "commit",
    "config",
    "plan",
    "checks",
    "workflow-paths",
    "workflow-security-profile",
    "workflow-security-rules",
    "mode-policy",
    "path-policy",
    "size-policy",
    "tree-policy",
)

NON_CLI_INPUT_NAMES = (
    "trusted-config",
    "gate",
    "operation",
    "prerequisite-workflow",
    "lifecycle-workflow",
    "owner-id",
    "approval-marker",
    "request-timeout",
    "job-timeout-minutes",
    "agent-review-policy-file",
)

_ALLOWED_INPUTS_BY_CHECK: Mapping[str, frozenset[str]] = {
    "branch": frozenset({"policy", "branch"}),
    "change": frozenset({"policy", "range", "repo"}),
    "mode": frozenset({"policy", "revision", "repo"}),
    "path": frozenset({"policy", "revision", "repo"}),
    "policy": frozenset({"policy", "repo"}),
    "pr-description": frozenset({"policy", "template", "file", "pr", "repo"}),
    "pr-review": frozenset(
        {"policy", "file", "pr", "repo", "wait-seconds", "submit-review", "triage-policy"}
    ),
    "pr-triage": frozenset({"policy", "file", "pr", "repo", "publish-comment"}),
    "repo": frozenset(
        {
            "plan",
            "checks",
            "repo",
            "config",
            "commit",
            "range",
            "revision",
            "workflow-paths",
            "workflow-security-profile",
            "workflow-security-rules",
            "mode-policy",
            "path-policy",
            "size-policy",
            "tree-policy",
        }
    ),
    "size": frozenset({"policy", "revision", "repo"}),
    "tree": frozenset({"policy", "revision", "repo"}),
    "workflow": frozenset({"workflow-paths", "repo"}),
    "workflow-lint": frozenset({"workflow-paths", "repo"}),
    "workflow-security": frozenset(
        {"workflow-paths", "repo", "workflow-security-profile", "workflow-security-rules"}
    ),
}


def _configure_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _env_key(input_name: str) -> str:
    return f"YAGA_INPUT_{input_name.upper().replace('-', '_')}"


def _read_input(environ: Mapping[str, str], name: str) -> str:
    raw = environ.get(_env_key(name), "")
    if "\x00" in raw:
        raise InputError(f"action input '{name}' contains a NUL byte")
    if len(raw.encode("utf-8", errors="replace")) > MAX_ACTION_INPUT_BYTES:
        raise InputError(f"action input '{name}' exceeds 64 KiB")
    return raw.strip()


def _parse_token_list(raw: str, *, name: str) -> tuple[str, ...]:
    if not raw:
        return ()
    tokens: list[str] = []
    for line in raw.splitlines():
        stripped_line = line.strip()
        if not stripped_line:
            continue
        for segment in stripped_line.split(","):
            token = segment.strip()
            if not token:
                raise InputError(f"action input '{name}' contains an empty list item")
            if any(char.isspace() for char in token):
                raise InputError(f"action input '{name}' items must not contain whitespace")
            if token.startswith("-"):
                raise InputError(f"action input '{name}' items must not start with '-'")
            tokens.append(token)
            if len(tokens) > MAX_ACTION_LIST_ITEMS:
                raise InputError(f"action input '{name}' exceeds {MAX_ACTION_LIST_ITEMS} items")
    return tuple(tokens)


def _parse_newline_paths(raw: str, *, name: str) -> tuple[str, ...]:
    if not raw:
        return ()
    paths: list[str] = []
    for line in raw.splitlines():
        item = line.strip()
        if not item:
            continue
        if item.startswith("-"):
            raise InputError(f"action input '{name}' entries must not start with '-'")
        paths.append(item)
        if len(paths) > MAX_ACTION_LIST_ITEMS:
            raise InputError(f"action input '{name}' exceeds {MAX_ACTION_LIST_ITEMS} entries")
    return tuple(paths)


def _require_input(inputs: Mapping[str, str], name: str, *, check: str) -> str:
    value = inputs.get(name, "")
    if not value:
        raise InputError(f"check '{check}' requires input '{name}'")
    return value


def build_cli_invocation(
    environ: Mapping[str, str],
) -> tuple[list[str], dict[str, str], Path]:
    """Validate Action inputs and construct the isolated YAGA CLI command."""
    check = _read_input(environ, "check")
    if check not in CLI_ACTION_CHECKS:
        raise InputError("action check mode is invalid")

    if environ.get("YAGA_NON_CLI_INPUTS", "0") != "0":
        raise InputError(f"check '{check}' does not accept commit or gate inputs")
    for forbidden in NON_CLI_INPUT_NAMES:
        if _read_input(environ, forbidden):
            raise InputError(f"check '{check}' does not accept input '{forbidden}'")

    inputs = {name: _read_input(environ, name) for name in ALL_CLI_INPUT_NAMES}
    allowed = _ALLOWED_INPUTS_BY_CHECK[check]
    for name, value in inputs.items():
        if value and name not in allowed:
            raise InputError(f"check '{check}' does not accept input '{name}'")

    has_token_flag = environ.get("YAGA_HAS_GITHUB_TOKEN", "0")
    if has_token_flag not in {"0", "1"}:
        raise InputError("action token presence flag is invalid")
    gh_token = environ.get("YAGA_GH_TOKEN", "")
    if (
        "\x00" in gh_token
        or len(gh_token.encode("utf-8", errors="replace")) > MAX_ACTION_INPUT_BYTES
    ):
        raise InputError("github-token is invalid")
    has_token = has_token_flag == "1" or bool(gh_token)

    needs_gh_token = check in {"pr-review", "pr-description", "pr-triage"} and bool(inputs["pr"])
    if has_token and not needs_gh_token:
        raise InputError(f"check '{check}' does not accept input 'github-token' in this mode")
    if needs_gh_token and not gh_token.strip():
        raise InputError(f"check '{check}' with 'pr' requires input 'github-token'")

    workspace_raw = environ.get("GITHUB_WORKSPACE", "").strip()
    if not workspace_raw or "\x00" in workspace_raw:
        raise InputError("GITHUB_WORKSPACE is required for the composite Action")
    workspace = Path(workspace_raw)
    if not workspace.is_dir():
        raise InputError("GITHUB_WORKSPACE must reference an existing directory")

    arguments = _build_arguments(check, inputs, environ=environ)
    child_env = {
        key: value
        for key, value in environ.items()
        if not key.startswith("YAGA_")
        and not key.upper().startswith("PYTHON")
        and key not in {"GITHUB_TOKEN", "GH_TOKEN"}
    }
    if needs_gh_token:
        child_env["GH_TOKEN"] = gh_token.strip()

    return arguments, child_env, workspace


def _build_arguments(
    check: str,
    inputs: Mapping[str, str],
    *,
    environ: Mapping[str, str],
) -> list[str]:
    if check == "pr-triage":
        policy = _require_input(inputs, "policy", check=check)
        file_val = inputs["file"]
        pr_val = inputs["pr"]
        publish = inputs["publish-comment"]
        if publish not in {"", "true", "false"}:
            raise InputError("check 'pr-triage' publish-comment must be true or false")
        if bool(file_val) == bool(pr_val):
            raise InputError("check 'pr-triage' requires exactly one of 'file' or 'pr'")
        command = ["pull-request", "triage", "check", f"--policy={policy}"]
        if file_val:
            if inputs["repo"] or publish == "true":
                raise InputError(
                    "check 'pr-triage' offline file cannot use repo or publish-comment"
                )
            command.append(f"--file={file_val}")
        else:
            command.append(f"--pr={pr_val}")
            if inputs["repo"]:
                command.append(f"--repo={inputs['repo']}")
            if publish == "true":
                command.append("--publish-comment")
        command.append("--format=github")
        return command

    if check == "pr-review":
        policy = _require_input(inputs, "policy", check=check)
        file_val = inputs["file"]
        pr_val = inputs["pr"]
        if bool(file_val) == bool(pr_val):
            raise InputError("check 'pr-review' requires exactly one of 'file' or 'pr'")
        if file_val:
            for live_only in ("repo", "wait-seconds", "submit-review", "triage-policy"):
                if inputs[live_only]:
                    raise InputError(
                        f"check 'pr-review' with 'file' does not accept input '{live_only}'"
                    )
            return [
                "pull-request",
                "review",
                "check",
                f"--policy={policy}",
                f"--file={file_val}",
                "--format=github",
            ]
        command = [
            "pull-request",
            "review",
            "check",
            f"--policy={policy}",
            f"--pr={pr_val}",
        ]
        if inputs["repo"]:
            command.append(f"--repo={inputs['repo']}")
        if inputs["wait-seconds"]:
            command.append(f"--wait-seconds={inputs['wait-seconds']}")
        if inputs["submit-review"]:
            command.append(f"--submit-review={inputs['submit-review']}")
        if inputs["triage-policy"]:
            command.append(f"--triage-policy={inputs['triage-policy']}")
        command.append("--format=github")
        return command

    if check == "pr-description":
        policy = _require_input(inputs, "policy", check=check)
        file_val = inputs["file"]
        pr_val = inputs["pr"]
        if file_val and pr_val:
            raise InputError("check 'pr-description' accepts at most one of 'file' or 'pr'")
        template_args = [f"--template={inputs['template']}"] if inputs["template"] else []
        if pr_val:
            command = [
                "pull-request",
                "description",
                "check",
                f"--policy={policy}",
                f"--pr={pr_val}",
            ]
            if inputs["repo"]:
                command.append(f"--repo={inputs['repo']}")
            command.extend(template_args)
            command.append("--format=github")
            return command
        if inputs["repo"]:
            raise InputError("check 'pr-description' accepts 'repo' only with 'pr'")
        if file_val:
            return [
                "pull-request",
                "description",
                "check",
                f"--policy={policy}",
                f"--file={file_val}",
                *template_args,
                "--format=github",
            ]
        event_path = environ.get("GITHUB_EVENT_PATH", "").strip()
        if not event_path or "\x00" in event_path:
            raise InputError(
                "GITHUB_EVENT_PATH is required for event-based 'pr-description' checks"
            )
        return [
            "github",
            "pull-request",
            "description",
            "check",
            f"--policy={policy}",
            f"--event-file={event_path}",
            *template_args,
            "--format=github",
        ]

    if check == "branch":
        policy = _require_input(inputs, "policy", check=check)
        branch = _require_input(inputs, "branch", check=check)
        return [
            "branch",
            "check",
            f"--policy={policy}",
            f"--name={branch}",
            "--format=github",
        ]

    if check == "policy":
        command = ["policy", "check"]
        if inputs["repo"]:
            command.append(f"--repo={inputs['repo']}")
        command.append("--strict")
        command.append("--format=github")
        if inputs["policy"]:
            command.extend(("--", inputs["policy"]))
        return command

    if check == "change":
        policy = _require_input(inputs, "policy", check=check)
        revision_range = _require_input(inputs, "range", check=check)
        command = [
            "change",
            "check",
            f"--policy={policy}",
            f"--range={revision_range}",
        ]
        if inputs["repo"]:
            command.append(f"--repo={inputs['repo']}")
        command.append("--format=github")
        return command

    if check in {"mode", "path", "size", "tree"}:
        policy = _require_input(inputs, "policy", check=check)
        revision = _require_input(inputs, "revision", check=check)
        command = [
            check,
            "check",
            f"--policy={policy}",
            f"--revision={revision}",
        ]
        if inputs["repo"]:
            command.append(f"--repo={inputs['repo']}")
        command.append("--format=github")
        return command

    if check in {"workflow", "workflow-lint", "workflow-security"}:
        subcommand = (
            "check" if check == "workflow" else ("lint" if check == "workflow-lint" else "security")
        )
        workflow_paths = _parse_newline_paths(inputs["workflow-paths"], name="workflow-paths")
        command = ["workflow", subcommand]
        if inputs["repo"]:
            command.append(f"--repo={inputs['repo']}")
        if check == "workflow-security":
            if inputs["workflow-security-profile"]:
                command.append(f"--profile={inputs['workflow-security-profile']}")
            for rule in _parse_token_list(
                inputs["workflow-security-rules"], name="workflow-security-rules"
            ):
                command.append(f"--rule={rule}")
        command.append("--format=github")
        if workflow_paths:
            command.extend(("--", *workflow_paths))
        return command

    # check == "repo"
    checks_list = _parse_token_list(inputs["checks"], name="checks")
    workflow_paths = _parse_newline_paths(inputs["workflow-paths"], name="workflow-paths")
    security_rules = _parse_token_list(
        inputs["workflow-security-rules"], name="workflow-security-rules"
    )
    if not inputs["plan"] and not checks_list:
        raise InputError("check 'repo' requires 'plan' or 'checks'")
    command = ["repo", "check"]
    if inputs["plan"]:
        command.append(f"--plan={inputs['plan']}")
    for provider in checks_list:
        command.append(f"--check={provider}")
    if inputs["repo"]:
        command.append(f"--repo={inputs['repo']}")
    if inputs["config"]:
        command.append(f"--config={inputs['config']}")
    if inputs["commit"]:
        command.append(f"--commit={inputs['commit']}")
    if inputs["range"]:
        command.append(f"--range={inputs['range']}")
    if inputs["revision"]:
        command.append(f"--revision={inputs['revision']}")
    for path in workflow_paths:
        command.append(f"--workflow-path={path}")
    if inputs["workflow-security-profile"]:
        command.append(f"--workflow-security-profile={inputs['workflow-security-profile']}")
    for rule in security_rules:
        command.append(f"--workflow-security-rule={rule}")
    if inputs["mode-policy"]:
        command.append(f"--mode-policy={inputs['mode-policy']}")
    if inputs["path-policy"]:
        command.append(f"--path-policy={inputs['path-policy']}")
    if inputs["size-policy"]:
        command.append(f"--size-policy={inputs['size-policy']}")
    if inputs["tree-policy"]:
        command.append(f"--tree-policy={inputs['tree-policy']}")
    command.append("--format=github")
    return command


def _github_escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _emit_error(error: InputError) -> int:
    message = _github_escape(safe_error_text(error))
    print(f"::error title=YAGA action::YAGA input error: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    """Validate composite Action CLI inputs and execute the target YAGA command."""
    _configure_utf8_output()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        return _emit_error(InputError("cli action accepts no positional arguments"))
    try:
        cli_arguments, child_env, workspace = build_cli_invocation(os.environ)
    except InputError as error:
        return _emit_error(error)

    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "yaga", *cli_arguments],
            cwd=workspace,
            env=child_env,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=MAX_CLI_ACTION_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _emit_error(InputError("CLI action check exceeded its time limit"))
    except OSError:
        return _emit_error(InputError("CLI action failed to start the YAGA process"))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
