"""Installed test planning contracts with real explicit policy and Git inputs."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from yaga.cli import app
from yaga.commands import test as test_commands
from yaga.errors import ConfigurationError, InputError
from yaga.test_planning import reporting, service

runner = CliRunner()
EXAMPLES = Path(__file__).resolve().parents[1] / "docs/designs/examples/test-planning"
POLICY = """test-policy-version = 1
full-on = ["uv.lock"]
no-impact = ["docs/**"]

[[suites]]
id = "smoke"
binding = "repository-smoke"

[[suites]]
id = "backend"
binding = "backend-unit"
variants = ["py315", "py312"]
needs = ["smoke"]

[[suites]]
id = "frontend"
binding = "frontend-unit"

[profiles.development]
mode = "selective"
always = ["smoke"]

[profiles.integration]
mode = "full"

[[rules]]
id = "backend"
when-any = ["backend/**"]
select = ["backend"]

[[rules]]
id = "frontend"
when-any = ["frontend/**"]
select = ["frontend"]
"""


def _git(repository: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *args], text=True, stderr=subprocess.PIPE
    ).strip()


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "YAGA Tests")
    _git(repository, "config", "user.email", "yaga@example.invalid")
    _git(repository, "commit", "--allow-empty", "-m", "chore: initialize")
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "backend").mkdir()
    (repository / "backend" / "worker.py").write_text("# source\n", encoding="utf-8")
    _git(repository, "add", "backend/worker.py")
    _git(repository, "commit", "-m", "feat: add worker")
    head = _git(repository, "rev-parse", "HEAD")
    policy = tmp_path / "policy.toml"
    policy.write_text(POLICY, encoding="utf-8")
    return repository, policy, base, head


def _arguments(repository: Path, policy: Path, profile: str = "development") -> list[str]:
    return [
        "test",
        "plan",
        "--policy",
        str(policy),
        "--profile",
        profile,
        "--repo",
        str(repository),
        "--format",
        "json",
    ]


def _assert_error(result: Result, kind: str) -> dict[str, Any]:
    assert result.exit_code == 2
    assert result.stdout == ""
    document = json.loads(result.stderr)
    assert document["status"] == "error"
    assert document["valid"] is False
    assert document["kind"] == kind
    assert "plan" not in document
    assert "plan_digest" not in document
    return document


def _visible_text(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def test_help_exposes_only_installed_planning_commands() -> None:
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0
    assert "test" in root.stdout
    group = runner.invoke(app, ["test", "--help"])
    assert group.exit_code == 0
    assert "policy" in group.stdout and "plan" in group.stdout
    for deferred in ("evaluate", "execute", "collect"):
        assert deferred not in group.stdout
    help_result = runner.invoke(app, ["test", "plan", "--help"])
    assert help_result.exit_code == 0
    text = _visible_text(help_result.stdout)
    for option in (
        "--policy",
        "--profile",
        "--repo",
        "--range",
        "--revision",
        "--bindings-sha256",
        "--format",
        "--quiet",
    ):
        assert option in text
    for unsupported in ("--staged", "--working-tree", "--token", "--execute"):
        assert unsupported not in text


@pytest.mark.parametrize("missing", ["policy", "profile"])
def test_required_policy_and_profile_have_no_defaults(missing: str) -> None:
    args = ["test", "plan", "--revision", "HEAD"]
    if missing != "policy":
        args.extend(("--policy", "policy.toml"))
    if missing != "profile":
        args.extend(("--profile", "integration"))
    result = runner.invoke(app, args, color=True)
    assert result.exit_code == 2
    assert f"--{missing}" in _visible_text(result.stderr)


def test_policy_check_works_without_git_and_emits_exact_input_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "policy.toml"
    policy.write_text(POLICY, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    result = runner.invoke(
        app, ["test", "policy", "check", "--file", str(policy), "--format", "json"]
    )
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["kind"] == "test_policy"
    assert document["status"] == "valid"
    assert document["policy_sha256"] == hashlib.sha256(policy.read_bytes()).hexdigest()
    assert document["profiles"] == ["development", "integration"]
    assert document["suites"] == 3 and document["cells"] == 4
    assert document["rules"] == 2


def test_policy_check_requires_explicit_file_and_preserves_strict_validation(
    tmp_path: Path,
) -> None:
    result = runner.invoke(app, ["test", "policy", "check"], color=True)
    assert result.exit_code == 2
    assert "--file" in _visible_text(result.stderr)
    invalid = tmp_path / "invalid.toml"
    invalid.write_text(POLICY + '\ncommand = "not permitted"\n', encoding="utf-8")
    result = runner.invoke(
        app, ["test", "policy", "check", "--file", str(invalid), "--format", "json", "--quiet"]
    )
    _assert_error(result, "test_policy")


def test_range_plan_binds_exact_source_complete_variants_and_digest(
    history: tuple[Path, Path, str, str],
) -> None:
    repository, policy, base, head = history
    args = _arguments(repository, policy) + [
        "--range",
        f"{base}...{head}",
        "--bindings-sha256",
        "d" * 64,
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert set(document) == {"test-plan-version", "plan", "plan_digest"}
    plan = document["plan"]
    assert plan["source"] == {
        "base_commit": base,
        "head_commit": head,
        "comparison_commit": base,
        "execution_commit": head,
        "execution_tree": _git(repository, "rev-parse", f"{head}^{{tree}}"),
    }
    assert plan["changed_paths"] == ["backend/worker.py"]
    assert plan["selected"] == [
        {"suite": "smoke", "variants": ["default"]},
        {"suite": "backend", "variants": ["py312", "py315"]},
    ]
    assert len(plan["inventory"]) == 3
    assert plan["bindings_sha256"] == "d" * 64
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert document["plan_digest"] == hashlib.sha256(canonical).hexdigest()
    assert runner.invoke(app, args).stdout == result.stdout


def test_full_revision_has_null_comparison_and_all_obligations(
    history: tuple[Path, Path, str, str],
) -> None:
    repository, policy, _, head = history
    result = runner.invoke(
        app, _arguments(repository, policy, "integration") + ["--revision", head]
    )
    assert result.exit_code == 0, result.stderr
    plan = json.loads(result.stdout)["plan"]
    assert plan["changed_paths"] is None
    assert plan["bindings_sha256"] is None
    assert plan["source"]["execution_commit"] == head
    for key in ("base_commit", "head_commit", "comparison_commit"):
        assert plan["source"][key] is None
    assert {item["suite"] for item in plan["selected"]} == {"backend", "frontend", "smoke"}
    assert plan["effective_mode"] == "full"


@pytest.mark.parametrize(
    "source", [[], ["--range", "HEAD..HEAD", "--revision", "HEAD"], ["--revision", "HEAD"]]
)
def test_missing_mixed_or_selective_revision_sources_fail_without_plan(
    history: tuple[Path, Path, str, str], source: list[str]
) -> None:
    repository, policy, _, _ = history
    result = runner.invoke(app, _arguments(repository, policy) + source)
    _assert_error(result, "test_plan")


@pytest.mark.parametrize("digest", ["", "A" * 64, "a" * 63, "g" * 64, "a" * 65])
def test_invalid_binding_identity_fails_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, digest: str
) -> None:
    def unexpected(*args: object, **kwargs: object) -> object:
        pytest.fail("invalid binding identity must not perform Git I/O")

    monkeypatch.setattr(service, "collect_test_source", unexpected)
    result = runner.invoke(
        app,
        _arguments(tmp_path, tmp_path / "absent")
        + ["--range", "A..B", "--bindings-sha256", digest],
    )
    document = _assert_error(result, "test_plan")
    assert document["error"]["kind"] == "input"


@pytest.mark.parametrize(
    "source", ["missing..HEAD", "HEAD...", "--all..HEAD", "HEAD:backend..HEAD"]
)
def test_git_errors_are_json_stderr_without_partial_plan(
    history: tuple[Path, Path, str, str], source: str
) -> None:
    repository, policy, _, _ = history
    result = runner.invoke(app, _arguments(repository, policy) + [f"--range={source}"])
    _assert_error(result, "test_plan")


def test_quiet_success_and_reporting_failure_do_not_leak_partial_plans(
    history: tuple[Path, Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, policy, _, head = history
    args = _arguments(repository, policy, "integration") + ["--revision", head, "--quiet"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0 and result.stdout == result.stderr == ""

    def too_large(*args: object, **kwargs: object) -> object:
        raise ConfigurationError("complete test plan exceeds limit")

    monkeypatch.setattr(service, "test_plan_document", too_large)
    result = runner.invoke(app, args)
    _assert_error(result, "test_plan")


def test_text_errors_sanitize_untrusted_input_even_when_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid(*args: object, **kwargs: object) -> object:
        raise InputError("bad\x1b[31m\nsource\u202e")

    monkeypatch.setattr(test_commands, "create_test_plan", invalid)
    result = runner.invoke(
        app,
        ["test", "plan", "--policy", "p.toml", "--profile", "dev", "--revision", "HEAD", "--quiet"],
    )
    assert result.exit_code == 2 and result.stdout == ""
    assert "\x1b" not in result.stderr and "\u202e" not in result.stderr
    assert len(result.stderr.splitlines()) == 1


@pytest.mark.parametrize("example", ["polyglot", "django-ray"])
def test_both_example_catalogs_work_through_policy_and_full_plan_commands(
    history: tuple[Path, Path, str, str], example: str
) -> None:
    repository, _, base, head = history
    policy = EXAMPLES / f"{example}.toml"
    checked = runner.invoke(
        app, ["test", "policy", "check", "--file", str(policy), "--format", "json"]
    )
    assert checked.exit_code == 0, checked.stderr
    policy_document = json.loads(checked.stdout)
    full = runner.invoke(app, _arguments(repository, policy, "integration") + ["--revision", head])
    assert full.exit_code == 0, full.stderr
    plan = json.loads(full.stdout)["plan"]
    assert len(plan["selected"]) == policy_document["suites"]
    assert sum(len(item["variants"]) for item in plan["selected"]) == policy_document["cells"]
    selective = runner.invoke(app, _arguments(repository, policy) + ["--range", f"{base}..{head}"])
    assert selective.exit_code == 0, selective.stderr
    assert json.loads(selective.stdout)["plan"]["changed_paths"] == ["backend/worker.py"]


def test_policy_path_and_revision_spelling_do_not_change_plan_identity(
    history: tuple[Path, Path, str, str],
) -> None:
    repository, policy, _, head = history
    copy = policy.with_name("copy.toml")
    copy.write_bytes(policy.read_bytes())
    first = runner.invoke(
        app, _arguments(repository, policy, "integration") + ["--revision", "HEAD"]
    )
    second = runner.invoke(app, _arguments(repository, copy, "integration") + ["--revision", head])
    assert first.exit_code == second.exit_code == 0
    assert first.stdout == second.stdout
    copy.write_bytes(copy.read_bytes() + b"\n# policy identity changes\n")
    third = runner.invoke(app, _arguments(repository, copy, "integration") + ["--revision", head])
    assert third.exit_code == 0
    assert json.loads(first.stdout)["plan_digest"] != json.loads(third.stdout)["plan_digest"]


def test_default_repository_is_cwd_without_inferring_event_or_source(
    history: tuple[Path, Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, policy, _, head = history
    monkeypatch.chdir(repository)
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(repository / "missing-event.json"))
    args = ["test", "plan", "--policy", str(policy), "--profile", "integration", "--format", "json"]
    _assert_error(runner.invoke(app, args), "test_plan")
    result = runner.invoke(app, args + ["--revision", head])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["plan"]["source"]["execution_commit"] == head


def test_only_text_and_json_formats_are_supported(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        _arguments(tmp_path, tmp_path / "policy") + ["--revision", "HEAD", "--format", "github"],
    )
    assert result.exit_code == 2
    assert "github" in result.stderr


@pytest.mark.parametrize("command", [["test", "policy", "check"], ["test", "plan"]])
def test_successful_text_output_describes_planning_not_passed_tests(
    history: tuple[Path, Path, str, str], command: list[str]
) -> None:
    repository, policy, _, head = history
    if command[-1] == "check":
        args = command + ["--file", str(policy)]
    else:
        args = command + [
            "--policy",
            str(policy),
            "--profile",
            "integration",
            "--revision",
            head,
            "--repo",
            str(repository),
        ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.stderr
    assert result.stdout and result.stderr == ""
    assert "tests passed" not in result.stdout.lower()


def test_invalid_profile_and_selective_revision_fail_before_source_collection(
    history: tuple[Path, Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, policy, _, head = history

    def unexpected(*args: object, **kwargs: object) -> object:
        pytest.fail("invalid profile/source combination must not inspect Git")

    monkeypatch.setattr(service, "collect_test_source", unexpected)
    for profile in ("missing", "development"):
        result = runner.invoke(app, _arguments(repository, policy, profile) + ["--revision", head])
        _assert_error(result, "test_plan")


def test_json_stdout_obeys_the_complete_report_byte_bound(
    history: tuple[Path, Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, policy, _, head = history
    args = _arguments(repository, policy, "integration") + ["--revision", head]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.stderr
    assert not first.stdout.endswith("\n")
    maximum = len(first.stdout.encode("utf-8"))
    monkeypatch.setattr(reporting, "MAX_PLAN_BYTES", maximum)
    exact = runner.invoke(app, args)
    assert exact.exit_code == 0
    assert len(exact.stdout.encode("utf-8")) == maximum
    monkeypatch.setattr(reporting, "MAX_PLAN_BYTES", maximum - 1)
    oversized = runner.invoke(app, args)
    _assert_error(oversized, "test_plan")
    quiet = runner.invoke(app, args + ["--quiet"])
    _assert_error(quiet, "test_plan")


def test_quiet_policy_success_suppresses_both_formats(history: tuple[Path, Path, str, str]) -> None:
    _, policy, _, _ = history
    for output_format in ("text", "json"):
        result = runner.invoke(
            app,
            [
                "test",
                "policy",
                "check",
                "--file",
                str(policy),
                "--format",
                output_format,
                "--quiet",
            ],
        )
        assert result.exit_code == 0
        assert result.stdout == result.stderr == ""
