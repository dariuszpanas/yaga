"""Build and inspect YAGA distributions with the locked backend."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import BinaryIO, cast

if __package__:
    from .check_description import validate_description
else:
    from check_description import validate_description

ROOT = Path(__file__).resolve().parents[1]
MAX_SMOKE_OUTPUT_BYTES = 65_536
MAX_DISTRIBUTION_METADATA_BYTES = 1024 * 1024
SMOKE_MESSAGE = "feat(build): execute the built wheel"
EXPECTED_REQUIRES_DIST = ["pygments<3,>=2.20.0", "pyyaml<7,>=6.0.3", "typer<1,>=0.27.2"]
EXPECTED_OPTIONAL_REQUIRES_DIST = [
    "boto3<2,>=1.37; extra == 'quality-bedrock'",
    "certifi>=2026.10.19; extra == 'quality'",
    "torch<3,>=2.13; extra == 'quality'",
    "transformers<6,>=5.10; extra == 'quality'",
]
DESCRIPTION_REMOTE_SMOKE_SCRIPT = """\
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner
from yaga.cli import app
from yaga.git.runtime import ProcessResult

url = "https://github.com/example/project/pull/7"
payload = {"body": "## Summary\\nExplain the changed behavior.", "number": 7, "url": url}
calls = []
def fetch(command, **options):
    assert command[1:] == ["pr", "view", "7", "--repo", "github.com/example/project", "--json", "body,number,url"]
    calls.append(command)
    return ProcessResult(0, json.dumps(payload).encode(), b"", False, False, False)

with tempfile.TemporaryDirectory() as directory:
    policy = Path(directory) / "policy.toml"
    policy.write_text("description-policy-version = 1\\n", encoding="utf-8")
    arguments = ["pull-request", "description", "check", "--policy", str(policy), "--format", "json"]
    runner = CliRunner()
    codes = []
    with patch("yaga.descriptions.github.shutil.which", return_value=sys.executable), patch("yaga.descriptions.github.run_bounded_process", side_effect=fetch):
        for source in (["--pr", url], ["--pr", "7", "--repo", "example/project"]):
            result = runner.invoke(app, arguments + source)
            assert result.exit_code == 0, result.stderr
            document = json.loads(result.stdout)
            assert document["source"] == url and document["pull_request_number"] == 7
            codes.append(result.exit_code)
        payload["body"] = "# Oversized heading style"
        result = runner.invoke(app, arguments + ["--pr", url])
        assert result.exit_code == 1, result.stderr
        assert "description.heading-level" in {item["code"] for item in json.loads(result.stdout)["diagnostics"]}
        codes.append(result.exit_code)
        payload["url"] = "https://github.com/example/other/pull/7"
        result = runner.invoke(app, arguments + ["--pr", url])
        assert result.exit_code == 2 and not result.stdout
        assert json.loads(result.stderr)["error"]["kind"] == "input"
        codes.append(result.exit_code)
print(json.dumps({"exit_codes": codes, "fetches": len(calls)}))
"""
TEST_PLANNING_SMOKE_SCRIPT = '''\
import hashlib
import json

from yaga.errors import ConfigurationError, InputError
from yaga.test_planning.diagnostics import diagnose_selection, encode_diagnostics, parse_plan_document
from yaga.test_planning.handoff import ExecutorRegistry, SuiteExecutor, create_handoff
from yaga.test_planning.planner import plan_tests
from yaga.test_planning.policy import parse_test_policy
from yaga.test_planning.reporting import test_plan_document
from yaga.test_planning.reports import PlannedTests
from yaga.test_planning.source import PlanSource

source = b"""
test-policy-version = 1
[[suites]]
id = "backend"
binding = "pytest"
tags = ["python"]
variants = ["py315", "py312"]
needs = ["build"]
[[suites]]
id = "build"
binding = "docker"
[[suites]]
id = "frontend"
binding = "npm"
[profiles.development]
mode = "selective"
[profiles.integration]
mode = "full"
[[rules]]
id = "backend"
when-any = ["src/**"]
select-tags = ["python"]
"""
policy = parse_test_policy(source)
selective = plan_tests(policy, profile="development", changed_paths=("src/module.py",))
full = plan_tests(policy, profile="integration", changed_paths=None)
fallback = plan_tests(policy, profile="development", changed_paths=("unknown.txt",))
invalid_policy_rejected = False
try:
    parse_test_policy(source.replace(b"test-policy-version = 1", b"test-policy-version = 2"))
except ConfigurationError:
    invalid_policy_rejected = True
registry = ExecutorRegistry((
    SuiteExecutor("backend", "pytest", "backend", ("py312", "py315"), (), "01" * 32),
    SuiteExecutor("build", "docker", "build", ("default",), (), "02" * 32),
    SuiteExecutor("frontend", "npm", "frontend", ("default",), (), "03" * 32),
))
source_identity = PlanSource("a" * 40, "b" * 40, "a" * 40, "b" * 40, "c" * 40)
policy_sha256 = hashlib.sha256(source).hexdigest()
planned = PlannedTests(policy_sha256, registry.sha256, source_identity, selective)
expected = dict(
    expected_policy_sha256=policy_sha256,
    expected_execution_commit="b" * 40,
    expected_execution_tree="c" * 40,
)
handoff = create_handoff(planned, registry, **expected)
diagnostics = diagnose_selection(planned, baseline_suites=("backend", "frontend"))
diagnostic_bytes = encode_diagnostics(diagnostics)
round_trip = parse_plan_document(test_plan_document(planned)) == planned
rejected_handoffs = []
for candidate in (
    PlannedTests(policy_sha256, None, source_identity, selective),
    PlannedTests(policy_sha256, "ff" * 32, source_identity, selective),
):
    try:
        create_handoff(candidate, registry, **expected)
    except InputError:
        rejected_handoffs.append(True)
try:
    create_handoff(planned, registry, **dict(expected, expected_execution_tree="d" * 40))
except InputError:
    rejected_handoffs.append(True)
print(json.dumps({
    "selective": [[item.suite, list(item.variants)] for item in selective.selected],
    "full": [item.suite for item in full.selected],
    "fallback": [item.suite for item in fallback.selected],
    "modes": [selective.effective_mode, full.effective_mode, fallback.effective_mode],
    "invalid_policy_rejected": invalid_policy_rejected,
    "handoff_version": handoff["test-handoff-version"],
    "dispatch": handoff["dispatch"],
    "cells": [[cell["suite"], cell["variant"]] for cell in handoff["cells"]],
    "rejected_handoffs": rejected_handoffs,
    "diagnostics": {
        "version": diagnostics["test-selection-diagnostics-version"],
        "breadth": diagnostics["selection_breadth"],
        "cause": diagnostics["fallback_cause"],
        "comparison": diagnostics["comparison"],
        "round_trip": round_trip and json.loads(diagnostic_bytes) == diagnostics,
    },
}))
'''
TEST_PLANNING_CLI_POLICY = b"""\
test-policy-version = 1
no-impact = ["tests/**"]
[[suites]]
id = "backend"
binding = "pytest"
variants = ["py315", "py312"]
needs = ["build"]
[[suites]]
id = "build"
binding = "docker"
[[suites]]
id = "frontend"
binding = "npm"
[profiles.development]
mode = "selective"
[profiles.integration]
mode = "full"
[[rules]]
id = "backend"
when-any = ["src/**"]
select = ["backend"]
"""


def validate_wheel_metadata(raw: bytes) -> None:
    """Require the wheel to advertise its complete supported runtime contract."""
    metadata = BytesParser().parsebytes(raw)
    requires_dist = metadata.get_all("Requires-Dist", [])
    runtime = [item for item in requires_dist if "; extra ==" not in item]
    optional = sorted(item for item in requires_dist if "; extra ==" in item)
    if runtime != EXPECTED_REQUIRES_DIST or optional != EXPECTED_OPTIONAL_REQUIRES_DIST:
        raise SystemExit("wheel has the wrong runtime dependency metadata")
    if metadata.get("Requires-Python") != ">=3.12":
        raise SystemExit("wheel has the wrong Python requirement metadata")
    if metadata.get("Description-Content-Type") != "text/markdown":
        raise SystemExit("wheel must declare a Markdown description")
    payload = metadata.get_payload(decode=True)
    if not isinstance(payload, bytes):
        raise SystemExit("wheel must contain a UTF-8 description")
    validate_description(payload.decode("utf-8"))


def validate_sdist_metadata(source_distribution: tarfile.TarFile) -> None:
    """Require one bounded root metadata file with the same contract as the wheel."""
    members = [
        member
        for member in source_distribution.getmembers()
        if member.name.rsplit("/", 1)[-1] == "PKG-INFO"
    ]
    if len(members) != 1:
        raise SystemExit("source distribution must contain exactly one metadata document")
    member = members[0]
    if (
        member.name.count("/") != 1
        or not member.name.startswith("yaga_cli-")
        or not member.isfile()
        or not 0 < member.size <= MAX_DISTRIBUTION_METADATA_BYTES
    ):
        raise SystemExit("source distribution metadata must be a bounded regular root file")
    metadata_file = source_distribution.extractfile(member)
    if metadata_file is None:
        raise SystemExit("source distribution metadata could not be read")
    with metadata_file:
        raw = metadata_file.read(MAX_DISTRIBUTION_METADATA_BYTES + 1)
    if len(raw) != member.size:
        raise SystemExit("source distribution metadata size does not match its header")
    validate_wheel_metadata(raw)


def run_bounded(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    """Run one smoke command with hard per-stream byte and time limits."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()

    def drain(stream: BinaryIO, target: bytearray) -> None:
        while chunk := stream.read(8192):
            remaining = MAX_SMOKE_OUTPUT_BYTES - len(target)
            if remaining > 0:
                target.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return

    threads = [
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        returncode = process.wait()
    finally:
        for thread in threads:
            thread.join()
    if timed_out:
        raise SystemExit("installed wheel CLI exceeded the smoke-test timeout")
    if overflow.is_set():
        raise SystemExit("installed wheel CLI output exceeds the smoke-test limit")
    return subprocess.CompletedProcess(command, returncode, bytes(stdout), bytes(stderr))


def successful_json(
    completed: subprocess.CompletedProcess[bytes],
    *,
    operation: str,
) -> dict[str, object]:
    """Require one installed CLI operation to succeed with UTF-8 JSON only."""
    if completed.returncode != 0:
        raise SystemExit(
            f"installed wheel CLI {operation} exited with status {completed.returncode}"
        )
    if completed.stderr:
        raise SystemExit(f"installed wheel CLI {operation} wrote to standard error")
    try:
        document = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"installed wheel CLI {operation} did not emit UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise SystemExit(f"installed wheel CLI {operation} emitted a non-object JSON report")
    return document


def run_smoke_git(git: str, consumer: Path, arguments: list[str]) -> str:
    """Keep disposable Git setup independent of ambient repository and configuration state."""
    environment = {
        name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
    )
    completed = run_bounded([git, "-C", str(consumer), *arguments], cwd=consumer, env=environment)
    if completed.returncode != 0:
        raise SystemExit("installed wheel disposable Git setup failed")
    return completed.stdout.decode("utf-8").strip()


def exercise_installed_description(
    executable: Path, *, consumer: Path, env: dict[str, str]
) -> None:
    """Exercise local and event description checks before creating the consumer repository."""
    policy = consumer.parent / "description-policy.toml"
    template = consumer.parent / "pull-request-template.md"
    body = consumer.parent / "pull-request-description.md"
    event = consumer.parent / "pull-request-event.json"
    policy.write_text(
        "description-policy-version = 1\nmin-words = 5\n",
        encoding="utf-8",
        newline="\n",
    )
    template.write_text(
        "## Summary\n\n<!-- Explain the change. -->\n\n## Validation\n",
        encoding="utf-8",
        newline="\n",
    )
    description = (
        "## Summary\n\nReject empty descriptions before review.\n\n"
        "## Validation\n\nRan the description checks successfully.\n"
    )
    body.write_text(description, encoding="utf-8", newline="\n")
    arguments = [
        str(executable),
        "pull-request",
        "description",
        "check",
        "--policy",
        str(policy),
        "--template",
        str(template),
        "--file",
        str(body),
        "--format",
        "json",
    ]
    completed = run_bounded(arguments, cwd=consumer, env=env)
    document = successful_json(completed, operation="pull-request description check")
    if not (
        document.get("schema_version") == 1
        and document.get("kind") == "pull_request_description"
        and document.get("status") == "passed"
        and document.get("valid") is True
        and document.get("policy_path") == str(policy.resolve())
        and document.get("template_path") == str(template)
        and document.get("pull_request_number") is None
        and document.get("word_count") == 10
        and document.get("diagnostic_count") == 0
        and document.get("omitted_diagnostic_count") == 0
        and document.get("diagnostics") == []
    ):
        raise SystemExit("installed wheel description check emitted the wrong report contract")

    event.write_text(
        json.dumps(
            {
                "action": "edited",
                "number": 7,
                "pull_request": {
                    "number": 7,
                    "state": "open",
                    "draft": False,
                    "body": description,
                },
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    event_arguments = [str(executable), "github", *arguments[1:]]
    source_option = event_arguments.index("--file")
    event_arguments[source_option : source_option + 2] = ["--event-file", str(event)]
    event_completed = run_bounded(event_arguments, cwd=consumer, env=env)
    event_document = successful_json(
        event_completed, operation="github pull-request description check"
    )
    if not (
        event_document.get("schema_version") == 1
        and event_document.get("kind") == "pull_request_description"
        and event_document.get("status") == "passed"
        and event_document.get("valid") is True
        and event_document.get("pull_request_number") == 7
        and event_document.get("diagnostics") == []
    ):
        raise SystemExit("installed wheel event description check emitted the wrong report")

    standalone_arguments = arguments.copy()
    template_option = standalone_arguments.index("--template")
    del standalone_arguments[template_option : template_option + 2]
    oversized_heading = "Description details " * 20
    for markdown, expected_codes in (
        (f"# {oversized_heading}", {"description.heading-level", "description.heading-length"}),
        (f"{oversized_heading}\n===", {"description.heading-level", "description.heading-length"}),
        (f"<h1>{oversized_heading}</h1>", {"description.html-heading"}),
        ("Explain \u200bthe change.", {"description.invisible-character"}),
        ("Explain\tthe change.", {"description.tab"}),
        (r"Explain the change.\n\nTests passed.", {"description.escaped-newline"}),
        ("Explain `the change.", {"description.backtick"}),
    ):
        body.write_text(
            f"{markdown}\n\nExplain the change in an ordinary paragraph.\n",
            encoding="utf-8",
            newline="\n",
        )
        malformed_description = run_bounded(standalone_arguments, cwd=consumer, env=env)
        if malformed_description.returncode != 1 or malformed_description.stderr:
            raise SystemExit(
                "installed wheel description check accepted malformed Markdown or prose"
            )
        malformed_report = json.loads(malformed_description.stdout.decode("utf-8"))
        if not expected_codes <= {item["code"] for item in malformed_report["diagnostics"]}:
            raise SystemExit("installed wheel description check omitted Markdown or prose findings")

    body.write_text("Explain the \x08ackend change.", encoding="utf-8", newline="\n")
    damaged = run_bounded(standalone_arguments, cwd=consumer, env=env)
    if damaged.returncode != 2 or damaged.stdout:
        raise SystemExit("installed wheel description check accepted a destructive control")
    damaged_error = json.loads(damaged.stderr.decode("utf-8"))
    if "U+0008" not in damaged_error["error"]["message"]:
        raise SystemExit("installed wheel description error omitted the damaged code point")

    body.write_text("## Summary\n\nOnly one section was filled.\n", encoding="utf-8", newline="\n")
    missing_section = run_bounded([*arguments, "--quiet"], cwd=consumer, env=env)
    if missing_section.returncode != 1 or missing_section.stdout or missing_section.stderr:
        raise SystemExit("installed wheel description check did not reject an incomplete template")
    policy.write_text(
        "description-policy-version = 1\nunknown-setting = true\n",
        encoding="utf-8",
        newline="\n",
    )
    malformed = run_bounded(arguments, cwd=consumer, env=env)
    if malformed.returncode != 2 or malformed.stdout:
        raise SystemExit("installed wheel description check did not reject an invalid policy")
    try:
        failure = json.loads(malformed.stderr.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit("installed wheel description error was not UTF-8 JSON") from error
    if not isinstance(failure, dict) or failure.get("status") != "error":
        raise SystemExit("installed wheel description check emitted the wrong error contract")


def exercise_installed_remote_description(
    python: Path, *, consumer: Path, env: dict[str, str]
) -> None:
    """Exercise the installed remote-source path with a controlled gh transport."""
    completed = run_bounded(
        [str(python), "-I", "-c", DESCRIPTION_REMOTE_SMOKE_SCRIPT], cwd=consumer, env=env
    )
    document = successful_json(completed, operation="remote description API smoke")
    if document != {"exit_codes": [0, 0, 1, 2], "fetches": 4}:
        raise SystemExit("installed wheel remote description check emitted the wrong result")


def exercise_installed_test_planning(python: Path, *, consumer: Path, env: dict[str, str]) -> None:
    """Preserve the pure policy/planning API in the installed distribution."""
    completed = run_bounded(
        [str(python), "-I", "-c", TEST_PLANNING_SMOKE_SCRIPT], cwd=consumer, env=env
    )
    document = successful_json(completed, operation="test planning API smoke")
    if document != {
        "selective": [["build", ["default"]], ["backend", ["py312", "py315"]]],
        "full": ["build", "backend", "frontend"],
        "fallback": ["build", "backend", "frontend"],
        "modes": ["selective", "full", "full"],
        "invalid_policy_rejected": True,
        "handoff_version": 1,
        "dispatch": {"backend": True, "build": True, "frontend": False},
        "cells": [["build", "default"], ["backend", "py312"], ["backend", "py315"]],
        "rejected_handoffs": [True, True, True],
        "diagnostics": {
            "version": 1,
            "breadth": "partial",
            "cause": "selective",
            "comparison": {
                "baseline": ["backend", "frontend"],
                "selected": ["backend", "build"],
                "additional": ["build"],
                "missing": ["frontend"],
            },
            "round_trip": True,
        },
    }:
        raise SystemExit("installed wheel test planning API emitted the wrong selection")


def exercise_installed_test_policy(
    executable: Path, *, consumer: Path, env: dict[str, str]
) -> Path:
    """Validate explicit policy through the installed CLI outside any repository."""
    policy = consumer.parent / "test-policy.toml"
    policy.write_bytes(TEST_PLANNING_CLI_POLICY)
    completed = run_bounded(
        [
            str(executable),
            "test",
            "policy",
            "check",
            "--file",
            str(policy),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=env,
    )
    document = successful_json(completed, operation="test policy check")
    if document != {
        "schema_version": 1,
        "kind": "test_policy",
        "status": "valid",
        "valid": True,
        "test-policy-version": 1,
        "policy_sha256": hashlib.sha256(TEST_PLANNING_CLI_POLICY).hexdigest(),
        "suites": 3,
        "cells": 4,
        "profiles": ["development", "integration"],
        "rules": 1,
    }:
        raise SystemExit("installed wheel CLI test policy check emitted the wrong report contract")
    return policy


def exercise_installed_test_plan(
    executable: Path,
    *,
    consumer: Path,
    env: dict[str, str],
    policy: Path,
    base_sha: str,
    head_sha: str,
    tree_sha: str,
) -> None:
    """Require exact source-bound selections, digests, and error exits in the wheel."""
    bindings_sha256 = "ab" * 32
    for profile, source_option, source in (
        ("development", "--range", f"{base_sha}..{head_sha}"),
        ("integration", "--revision", head_sha),
    ):
        completed = run_bounded(
            [
                str(executable),
                "test",
                "plan",
                "--policy",
                str(policy),
                "--profile",
                profile,
                source_option,
                source,
                "--bindings-sha256",
                bindings_sha256,
                "--format",
                "json",
            ],
            cwd=consumer,
            env=env,
        )
        document = successful_json(completed, operation=f"test plan {profile}")
        plan = document.get("plan")
        if not isinstance(plan, dict):
            raise SystemExit("installed wheel CLI test plan omitted its semantic plan")
        digest = hashlib.sha256(
            json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        full = profile == "integration"
        expected_selection = [
            {"suite": "build", "variants": ["default"]},
            {"suite": "backend", "variants": ["py312", "py315"]},
        ]
        if full:
            expected_selection.append({"suite": "frontend", "variants": ["default"]})
        inventory = plan.get("inventory")
        if not (
            set(document) == {"test-plan-version", "plan_digest", "plan"}
            and document["test-plan-version"] == 1
            and document["plan_digest"] == digest
            and plan.get("test-plan-version") == 1
            and plan.get("policy_sha256") == hashlib.sha256(TEST_PLANNING_CLI_POLICY).hexdigest()
            and plan.get("bindings_sha256") == bindings_sha256
            and plan.get("profile") == profile
            and plan.get("requested_mode") == ("full" if full else "selective")
            and plan.get("effective_mode") == ("full" if full else "selective")
            and plan.get("source")
            == {
                "base_commit": None if full else base_sha,
                "head_commit": None if full else head_sha,
                "comparison_commit": None if full else base_sha,
                "execution_commit": head_sha,
                "execution_tree": tree_sha,
            }
            and plan.get("changed_paths")
            == (None if full else ["src/package.py", "tests/test_package.py"])
            and plan.get("selected") == expected_selection
            and isinstance(inventory, list)
            and [item.get("id") for item in inventory if isinstance(item, dict)]
            == ["backend", "build", "frontend"]
        ):
            raise SystemExit("installed wheel CLI test plan emitted the wrong report contract")
        if not full:
            exercise_installed_test_evaluate(
                executable,
                consumer=consumer,
                env=env,
                plan_document=document,
                head_sha=head_sha,
                tree_sha=tree_sha,
                bindings_sha256=bindings_sha256,
            )
    invalid = run_bounded(
        [
            str(executable),
            "test",
            "plan",
            "--policy",
            str(policy),
            "--profile",
            "development",
            "--range",
            f"{base_sha}..missing-test-plan-revision",
            "--format",
            "json",
        ],
        cwd=consumer,
        env=env,
    )
    if invalid.returncode != 2 or invalid.stdout:
        raise SystemExit("installed wheel CLI test plan accepted an invalid source")
    error_document = json.loads(invalid.stderr.decode("utf-8"))
    if not (
        isinstance(error_document, dict)
        and error_document.get("schema_version") == 1
        and isinstance(error_document.get("error"), dict)
        and error_document["error"].get("kind") == "git"
    ):
        raise SystemExit("installed wheel CLI test plan emitted the wrong error contract")


def exercise_installed_test_evaluate(
    executable: Path,
    *,
    consumer: Path,
    env: dict[str, str],
    plan_document: dict[str, object],
    head_sha: str,
    tree_sha: str,
    bindings_sha256: str,
) -> None:
    """Exercise installed receipt evaluation exits against independent fixture identities."""
    plan_path = consumer.parent / "evaluation-plan.json"
    results_path = consumer.parent / "evaluation-results.json"
    plan_path.write_text(json.dumps(plan_document), encoding="utf-8")
    policy_sha256 = hashlib.sha256(TEST_PLANNING_CLI_POLICY).hexdigest()
    results = {
        "test-results-version": 1,
        "plan_digest": plan_document["plan_digest"],
        "policy_sha256": policy_sha256,
        "bindings_sha256": bindings_sha256,
        "execution_commit": head_sha,
        "execution_tree": tree_sha,
        "run_id": "installed-wheel-smoke",
        "attempt": 1,
        "results": [
            {"suite": "backend", "variant": "py312", "binding": "pytest", "outcome": "passed"},
            {"suite": "backend", "variant": "py315", "binding": "pytest", "outcome": "passed"},
            {"suite": "build", "variant": "default", "binding": "docker", "outcome": "passed"},
        ],
    }
    results_path.write_text(json.dumps(results), encoding="utf-8")
    arguments = [
        str(executable),
        "test",
        "evaluate",
        "--plan",
        str(plan_path),
        "--results",
        str(results_path),
        "--policy-sha256",
        policy_sha256,
        "--bindings-sha256",
        bindings_sha256,
        "--execution-commit",
        head_sha,
        "--execution-tree",
        tree_sha,
        "--run-id",
        "installed-wheel-smoke",
        "--attempt",
        "1",
        "--format",
        "json",
    ]
    completed = run_bounded(arguments, cwd=consumer, env=env)
    passed = successful_json(completed, operation="test evaluate passed")
    if not (
        passed.get("test-evaluation-version") == 1
        and passed.get("status") == "passed"
        and passed.get("passed") is True
        and passed.get("complete") is True
    ):
        raise SystemExit("installed wheel CLI test evaluate emitted the wrong passing report")
    results["results"] = results["results"][:-1]
    results_path.write_text(json.dumps(results), encoding="utf-8")
    incomplete = run_bounded([*arguments, "--quiet"], cwd=consumer, env=env)
    if incomplete.returncode != 1 or incomplete.stderr:
        raise SystemExit("installed wheel CLI test evaluate accepted missing prerequisite results")
    incomplete_document = json.loads(incomplete.stdout.decode("utf-8"))
    if not (
        isinstance(incomplete_document, dict)
        and incomplete_document.get("status") == "incomplete"
        and incomplete_document.get("passed") is False
        and incomplete_document.get("complete") is False
    ):
        raise SystemExit("installed wheel CLI test evaluate hid an incomplete result")
    arguments[arguments.index("--run-id") + 1] = "displaced-run"
    invalid = run_bounded(arguments, cwd=consumer, env=env)
    if invalid.returncode != 2 or invalid.stdout:
        raise SystemExit("installed wheel CLI test evaluate accepted a displaced identity")
    error_document = json.loads(invalid.stderr.decode("utf-8"))
    if not (
        isinstance(error_document, dict)
        and error_document.get("kind") == "test_evaluation"
        and error_document.get("status") == "error"
    ):
        raise SystemExit("installed wheel CLI test evaluate emitted the wrong error contract")


def exercise_installed_wheel(uv: str, output: Path, wheel: Path) -> None:
    """Install the built wheel over its hash-locked runtime and execute its CLI."""
    runtime_requirements = output / "runtime-requirements.txt"
    export_command = [
        uv,
        "export",
        "--quiet",
        "--project",
        str(ROOT),
        "--locked",
        "--no-default-groups",
        "--no-emit-project",
        "--no-annotate",
        "--no-header",
        "--python",
        sys.executable,
        "--no-python-downloads",
        "--output-file",
        str(runtime_requirements),
    ]
    subprocess.run(
        export_command,
        cwd=ROOT,
        check=True,
    )

    environment = output / "wheel-environment"
    subprocess.run(
        [
            uv,
            "venv",
            "--no-project",
            "--no-config",
            "--no-python-downloads",
            "--python",
            sys.executable,
            str(environment),
        ],
        cwd=output,
        check=True,
    )
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(environment),
            "--no-config",
            "--no-python-downloads",
            "--exact",
            "--require-hashes",
            "--no-build",
            "--requirements",
            str(runtime_requirements),
        ],
        cwd=output,
        check=True,
    )
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(environment),
            "--no-config",
            "--no-python-downloads",
            "--no-deps",
            "--no-build",
            str(wheel.resolve()),
        ],
        cwd=output,
        check=True,
    )
    subprocess.run(
        [
            uv,
            "pip",
            "check",
            "--python",
            str(environment),
            "--no-config",
            "--no-python-downloads",
        ],
        cwd=output,
        check=True,
    )

    exercise_installed_runtime(environment, output)


def exercise_installed_runtime(environment: Path, output: Path) -> None:
    """Exercise the same installed CLI contracts with an already provisioned runtime."""
    consumer = output / "consumer"
    consumer.mkdir()
    config = consumer / ".yaga.toml"
    branch_policy = consumer / "branch-policy.toml"
    change_policy = consumer / "change-policy.toml"
    repository_plan = consumer / "repository-plan.toml"
    repository_plan_v2 = consumer / "repository-plan-v2.toml"
    mode_policy = consumer / "mode-policy.toml"
    path_policy = consumer / "path-policy.toml"
    size_policy = consumer / "size-policy.toml"
    tree_policy = consumer / "tree-policy.toml"
    executable = environment / ("Scripts/yaga.exe" if os.name == "nt" else "bin/yaga")
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        raise SystemExit("installed wheel does not expose the yaga executable")
    if not python.is_file():
        raise SystemExit("installed wheel environment does not expose its Python interpreter")
    child_environment = os.environ.copy()
    for variable in (
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "YAGA_ACTION_RUNTIME",
        "YAGA_COMMIT_ACTION_RUNTIME",
    ):
        child_environment.pop(variable, None)
    child_environment["PYTHONNOUSERSITE"] = "1"
    child_environment["PYTHONSAFEPATH"] = "1"
    exercise_installed_description(executable, consumer=consumer, env=child_environment)
    exercise_installed_remote_description(python, consumer=consumer, env=child_environment)
    exercise_installed_test_planning(python, consumer=consumer, env=child_environment)
    test_policy = exercise_installed_test_policy(
        executable, consumer=consumer, env=child_environment
    )
    branch_policy.write_text(
        'branch-policy-version = 1\nallowed-patterns = ["main", "feat/*", "fix/*"]\n',
        encoding="utf-8",
        newline="\n",
    )
    branch_completed = run_bounded(
        [
            str(executable),
            "branch",
            "check",
            "--policy",
            str(branch_policy),
            "--name",
            "feat/wheel-smoke",
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    branch_document = successful_json(branch_completed, operation="branch check")
    if not (
        branch_document.get("schema_version") == 1
        and branch_document.get("kind") == "branch_policy"
        and branch_document.get("status") == "passed"
        and branch_document.get("valid") is True
        and branch_document.get("policy_path") == str(branch_policy.resolve())
        and branch_document.get("branch") == "feat/wheel-smoke"
        and branch_document.get("allowed_patterns") == ["main", "feat/*", "fix/*"]
        and branch_document.get("matched_pattern") == "feat/*"
        and branch_document.get("diagnostics") == []
    ):
        raise SystemExit("installed wheel CLI branch check emitted the wrong report contract")
    initialized = run_bounded(
        [
            str(executable),
            "config",
            "init",
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    initialized_document = successful_json(initialized, operation="config init")
    initialized_policy = initialized_document.get("config")
    initialized_types = (
        initialized_policy.get("allowed_types") if isinstance(initialized_policy, dict) else None
    )
    if not (
        initialized_document.get("schema_version") == 1
        and initialized_document.get("config_path") == str(config.resolve())
        and isinstance(initialized_policy, dict)
        and isinstance(initialized_types, list)
        and "feat" in initialized_types
        and initialized_policy.get("dependabot_pull_requests") == "check"
        and initialized_policy.get("dependabot_require_github_signature") is True
        and initialized_policy.get("merge_commits") == "reject"
        and config.is_file()
    ):
        raise SystemExit("installed wheel CLI config init emitted the wrong report contract")

    workflow_directory = consumer / ".github" / "workflows"
    workflow_directory.mkdir(parents=True)
    (workflow_directory / "ci.yml").write_text(
        "name: CI\n"
        "on: push\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n",
        encoding="utf-8",
        newline="\n",
    )
    workflow_completed = run_bounded(
        [
            str(executable),
            "workflow",
            "check",
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    workflow_document = successful_json(workflow_completed, operation="workflow check")
    if not (
        workflow_document.get("schema_version") == 1
        and workflow_document.get("kind") == "github_workflow_policy"
        and workflow_document.get("valid") is True
        and workflow_document.get("checked") == 1
        and workflow_document.get("passed") == 1
        and workflow_document.get("failed") == 0
        and workflow_document.get("references_checked") == 1
    ):
        raise SystemExit("installed wheel CLI workflow check emitted the wrong report contract")

    security_completed = run_bounded(
        [
            str(executable),
            "workflow",
            "security",
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    security_document = successful_json(
        security_completed,
        operation="workflow security",
    )
    security_rules = security_document.get("rules")
    if not (
        security_document.get("schema_version") == 1
        and security_document.get("kind") == "github_workflow_security"
        and security_document.get("profile") == "recommended-v1"
        and security_document.get("valid") is True
        and security_document.get("checked") == 1
        and security_document.get("passed") == 1
        and security_document.get("failed") == 0
        and security_document.get("diagnostics") == 0
        and isinstance(security_rules, list)
        and len(security_rules) == 5
    ):
        raise SystemExit("installed wheel CLI workflow security emitted the wrong report contract")

    repository_plan.write_text(
        "plan-version = 1\n"
        'checks = ["workflow", "workflow-security"]\n'
        'workflow-paths = [".github/workflows"]\n'
        'workflow-security-profile = "recommended-v1"\n',
        encoding="utf-8",
        newline="\n",
    )
    repository_completed = run_bounded(
        [
            str(executable),
            "repo",
            "check",
            "--plan",
            str(repository_plan),
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    repository_document = successful_json(
        repository_completed,
        operation="repository check",
    )
    repository_checks = (
        repository_document.get("checks") if isinstance(repository_document, dict) else None
    )
    if not (
        repository_document.get("schema_version") == 1
        and repository_document.get("kind") == "repository_check"
        and repository_document.get("status") == "passed"
        and repository_document.get("valid") is True
        and repository_document.get("selected") == 2
        and repository_document.get("passed") == 2
        and repository_document.get("failed") == 0
        and repository_document.get("errored") == 0
        and isinstance(repository_checks, list)
        and len(repository_checks) == 2
        and isinstance(repository_checks[0], dict)
        and repository_checks[0].get("provider") == "workflow"
        and repository_checks[0].get("status") == "passed"
        and isinstance(repository_checks[1], dict)
        and repository_checks[1].get("provider") == "workflow-security"
        and repository_checks[1].get("status") == "passed"
    ):
        raise SystemExit("installed wheel CLI repository check emitted the wrong report contract")

    change_policy.write_text(
        "change-policy-version = 1\n\n"
        "[[rules]]\n"
        'name = "python-source-needs-tests"\n'
        'when-any = ["src/**/*.py"]\n'
        'require-any = ["tests/**/*.py"]\n',
        encoding="utf-8",
        newline="\n",
    )
    mode_policy.write_text(
        "mode-policy-version = 1\n"
        'default-allowed-modes = ["regular"]\n\n'
        "[[path-overrides]]\n"
        'pattern = "src/package.py"\n'
        'allowed-modes = ["executable"]\n',
        encoding="utf-8",
        newline="\n",
    )
    path_policy.write_text(
        'path-policy-version = 1\nprofile = "windows-compatible-v1"\n',
        encoding="utf-8",
        newline="\n",
    )
    size_policy.write_text(
        "size-policy-version = 1\n"
        "default-max-blob-bytes = 4096\n"
        "max-total-blob-bytes = 65536\n\n"
        "[[path-limits]]\n"
        'pattern = "src/**"\n'
        "max-blob-bytes = 128\n",
        encoding="utf-8",
        newline="\n",
    )
    tree_required_paths = [
        ".yaga.toml",
        "mode-policy.toml",
        "path-policy.toml",
        "repository-plan-v2.toml",
        "size-policy.toml",
        "tree-policy.toml",
        "src/package.py",
        "tests/test_package.py",
    ]
    tree_forbidden_patterns = ["**/.env", "**/*.pyc", "dist/**"]
    tree_policy.write_text(
        "tree-policy-version = 1\n"
        'required-paths = [".yaga.toml", "mode-policy.toml", "path-policy.toml", '
        '"repository-plan-v2.toml", '
        '"size-policy.toml", "tree-policy.toml", '
        '"src/package.py", "tests/test_package.py"]\n'
        'forbidden-patterns = ["**/.env", "**/*.pyc", "dist/**"]\n',
        encoding="utf-8",
        newline="\n",
    )
    repository_plan_v2.write_text(
        "plan-version = 2\n"
        'checks = ["commit", "mode", "path", "size", "tree"]\n'
        'mode-policy = "mode-policy.toml"\n'
        'path-policy = "path-policy.toml"\n'
        'size-policy = "size-policy.toml"\n'
        'tree-policy = "tree-policy.toml"\n',
        encoding="utf-8",
        newline="\n",
    )
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git is required for the installed change-policy smoke test")
    for arguments in (
        ("init", "--initial-branch=main", "--template="),
        ("config", "user.name", "YAGA Build"),
        ("config", "user.email", "yaga-build@example.invalid"),
        ("add", "--all"),
        (
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "--message",
            "chore: establish smoke repository",
        ),
    ):
        run_smoke_git(git, consumer, list(arguments))
    base_sha = run_smoke_git(git, consumer, ["rev-parse", "HEAD"])
    source = consumer / "src" / "package.py"
    test = consumer / "tests" / "test_package.py"
    source.parent.mkdir()
    test.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    test.write_text("def test_value():\n    assert 1 == 1\n", encoding="utf-8", newline="\n")
    run_smoke_git(git, consumer, ["add", "--all"])
    run_smoke_git(git, consumer, ["update-index", "--chmod=+x", "src/package.py"])
    run_smoke_git(
        git,
        consumer,
        [
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "--message",
            "test: cover smoke package",
        ],
    )
    head_sha = run_smoke_git(git, consumer, ["rev-parse", "HEAD"])
    tree_sha = run_smoke_git(git, consumer, ["rev-parse", f"{head_sha}^{{tree}}"])
    exercise_installed_test_plan(
        executable,
        consumer=consumer,
        env=child_environment,
        policy=test_policy,
        base_sha=base_sha,
        head_sha=head_sha,
        tree_sha=tree_sha,
    )
    size_expected_paths = sorted(
        (
            ".github/workflows/ci.yml",
            ".yaga.toml",
            "branch-policy.toml",
            "change-policy.toml",
            "mode-policy.toml",
            "repository-plan.toml",
            "repository-plan-v2.toml",
            "path-policy.toml",
            "size-policy.toml",
            "src/package.py",
            "tests/test_package.py",
            "tree-policy.toml",
        )
    )
    size_expected_largest: list[dict[str, object]] = []
    size_expected_total = 0
    for relative_path in size_expected_paths:
        oid = run_smoke_git(git, consumer, ["rev-parse", f"{head_sha}:{relative_path}"])
        committed_size = int(run_smoke_git(git, consumer, ["cat-file", "-s", oid]))
        size_expected_total += committed_size
        size_expected_largest.append(
            {
                "path": relative_path,
                "oid": oid,
                "mode": "100755" if relative_path == "src/package.py" else "100644",
                "size_bytes": committed_size,
                "max_blob_bytes": 128 if relative_path.startswith("src/") else 4096,
                "pattern": "src/**" if relative_path.startswith("src/") else None,
            }
        )
    size_expected_largest.sort(
        key=lambda item: (-cast(int, item["size_bytes"]), cast(str, item["path"])),
    )
    (consumer / ".env").write_text("UNTRACKED=1\n", encoding="utf-8", newline="\n")
    untracked_dist = consumer / "dist"
    untracked_dist.mkdir()
    (untracked_dist / "artifact.whl").write_bytes(b"untracked")
    tree_completed = run_bounded(
        [
            str(executable),
            "tree",
            "check",
            "--policy",
            str(tree_policy),
            "--revision",
            head_sha,
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    tree_document = successful_json(tree_completed, operation="tree check")
    if not (
        tree_document.get("schema_version") == 1
        and tree_document.get("kind") == "tree_policy"
        and tree_document.get("status") == "passed"
        and tree_document.get("valid") is True
        and tree_document.get("policy_path") == str(tree_policy.resolve())
        and tree_document.get("repository_path") == str(consumer.resolve())
        and tree_document.get("revision") == head_sha
        and tree_document.get("commit_sha") == head_sha
        and tree_document.get("tree_sha") == tree_sha
        and tree_document.get("entries_checked") == len(size_expected_paths)
        and tree_document.get("required_paths") == tree_required_paths
        and tree_document.get("forbidden_patterns") == tree_forbidden_patterns
        and tree_document.get("diagnostics") == []
        and tree_document.get("diagnostics_omitted") == 0
        and tree_document.get("missing_required") == 0
        and tree_document.get("forbidden_paths") == 0
    ):
        raise SystemExit("installed wheel CLI tree check emitted the wrong report contract")
    path_completed = run_bounded(
        [
            str(executable),
            "path",
            "check",
            "--policy",
            str(path_policy),
            "--revision",
            head_sha,
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    path_document = successful_json(path_completed, operation="path check")
    path_identity = path_document.get("identity")
    path_policy_report = path_document.get("policy")
    path_counts = path_document.get("counts")
    if not (
        path_document.get("schema_version") == 1
        and path_document.get("kind") == "path_policy"
        and path_document.get("status") == "passed"
        and path_document.get("valid") is True
        and path_identity
        == {
            "policy_path": str(path_policy.resolve()),
            "repository_path": str(consumer.resolve()),
            "revision": head_sha,
            "commit_sha": head_sha,
            "tree_sha": tree_sha,
        }
        and path_policy_report
        == {
            "path_policy_version": 1,
            "profile": "windows-compatible-v1",
            "rules": [
                "windows-characters",
                "windows-trailing",
                "windows-reserved",
                "ascii-case-collision",
            ],
        }
        and path_counts
        == {
            "paths": len(size_expected_paths),
            "components": sum(path.count("/") + 1 for path in size_expected_paths),
            "findings": 0,
            "by_code": {},
        }
        and path_document.get("diagnostics") == []
        and path_document.get("diagnostics_omitted") == 0
    ):
        raise SystemExit("installed wheel CLI path check emitted the wrong report contract")
    mode_completed = run_bounded(
        [
            str(executable),
            "mode",
            "check",
            "--policy",
            str(mode_policy),
            "--revision",
            head_sha,
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    mode_document = successful_json(mode_completed, operation="mode check")
    mode_identity = mode_document.get("identity")
    mode_policy_report = mode_document.get("policy")
    mode_counts = mode_document.get("counts")
    if not (
        mode_document.get("schema_version") == 1
        and mode_document.get("kind") == "mode_policy"
        and mode_document.get("status") == "passed"
        and mode_document.get("valid") is True
        and mode_identity
        == {
            "policy_path": str(mode_policy.resolve()),
            "repository_path": str(consumer.resolve()),
            "revision": head_sha,
            "commit_sha": head_sha,
            "tree_sha": tree_sha,
        }
        and mode_policy_report
        == {
            "mode_policy_version": 1,
            "default_allowed_modes": ["regular"],
            "path_overrides": [{"pattern": "src/package.py", "allowed_modes": ["executable"]}],
        }
        and mode_counts
        == {
            "entries": len(size_expected_paths),
            "by_mode": {
                "regular": len(size_expected_paths) - 1,
                "executable": 1,
                "symlink": 0,
                "gitlink": 0,
            },
            "findings": 0,
        }
        and mode_document.get("diagnostics") == []
        and mode_document.get("diagnostics_omitted") == 0
    ):
        raise SystemExit("installed wheel CLI mode check emitted the wrong report contract")
    size_completed = run_bounded(
        [
            str(executable),
            "size",
            "check",
            "--policy",
            str(size_policy),
            "--revision",
            head_sha,
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    size_document = successful_json(size_completed, operation="size check")
    size_identity = size_document.get("identity")
    size_policy_report = size_document.get("policy")
    size_counts = size_document.get("counts")
    size_total = size_document.get("total")
    size_largest = size_document.get("largest")
    if not (
        size_document.get("schema_version") == 1
        and size_document.get("kind") == "size_policy"
        and size_document.get("status") == "passed"
        and size_document.get("valid") is True
        and size_identity
        == {
            "policy_path": str(size_policy.resolve()),
            "repository_path": str(consumer.resolve()),
            "revision": head_sha,
            "commit_sha": head_sha,
            "tree_sha": tree_sha,
        }
        and size_policy_report
        == {
            "size_policy_version": 1,
            "default_max_blob_bytes": 4096,
            "max_total_blob_bytes": 65536,
            "path_limits": [{"pattern": "src/**", "max_blob_bytes": 128}],
        }
        and size_counts == {"blobs": len(size_expected_paths), "gitlinks": 0, "oversized_blobs": 0}
        and size_total
        == {
            "blob_bytes": size_expected_total,
            "max_total_blob_bytes": 65536,
            "exceeded": False,
        }
        and size_largest == {"blobs": size_expected_largest, "omitted": 0}
        and size_document.get("diagnostics") == []
        and size_document.get("diagnostics_omitted") == 0
    ):
        raise SystemExit("installed wheel CLI size check emitted the wrong report contract")
    repository_v2_completed = run_bounded(
        [
            str(executable),
            "repo",
            "check",
            "--plan",
            str(repository_plan_v2),
            "--repo",
            str(consumer),
            "--commit",
            head_sha,
            "--revision",
            head_sha,
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    repository_v2_document = successful_json(
        repository_v2_completed,
        operation="repository check plan v2",
    )
    repository_v2_checks = (
        repository_v2_document.get("checks") if isinstance(repository_v2_document, dict) else None
    )
    if not (
        repository_v2_document.get("schema_version") == 1
        and repository_v2_document.get("kind") == "repository_check"
        and repository_v2_document.get("status") == "passed"
        and repository_v2_document.get("valid") is True
        and repository_v2_document.get("selected") == 5
        and repository_v2_document.get("passed") == 5
        and repository_v2_document.get("failed") == 0
        and repository_v2_document.get("errored") == 0
        and isinstance(repository_v2_checks, list)
        and [item.get("provider") for item in repository_v2_checks if isinstance(item, dict)]
        == ["commit", "mode", "path", "size", "tree"]
    ):
        raise SystemExit("installed wheel CLI repository plan v2 emitted the wrong envelope")
    repository_v2_reports = {
        item["provider"]: item.get("report")
        for item in repository_v2_checks
        if isinstance(item, dict) and isinstance(item.get("provider"), str)
    }
    if not (
        isinstance(repository_v2_reports.get("commit"), dict)
        and repository_v2_reports["commit"].get("valid") is True
        and repository_v2_reports.get("mode") == mode_document
        and repository_v2_reports.get("path") == path_document
        and repository_v2_reports.get("size") == size_document
        and repository_v2_reports.get("tree") == tree_document
    ):
        raise SystemExit("installed wheel CLI repository plan v2 changed a standalone child report")
    change_completed = run_bounded(
        [
            str(executable),
            "change",
            "check",
            "--policy",
            str(change_policy),
            "--range",
            f"{base_sha}..{head_sha}",
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    change_document = successful_json(change_completed, operation="change check")
    change_rules = change_document.get("rules")
    if not (
        change_document.get("schema_version") == 1
        and change_document.get("kind") == "change_policy"
        and change_document.get("status") == "passed"
        and change_document.get("valid") is True
        and change_document.get("policy_path") == str(change_policy.resolve())
        and change_document.get("range") == f"{base_sha}..{head_sha}"
        and change_document.get("base_sha") == base_sha
        and change_document.get("head_sha") == head_sha
        and change_document.get("comparison_sha") == base_sha
        and change_document.get("paths_changed") == 2
        and change_document.get("paths") == ["src/package.py", "tests/test_package.py"]
        and change_document.get("paths_omitted") == 0
        and change_document.get("passed") == 1
        and change_document.get("failed") == 0
        and change_document.get("skipped") == 0
        and isinstance(change_rules, list)
        and len(change_rules) == 1
        and isinstance(change_rules[0], dict)
        and change_rules[0].get("name") == "python-source-needs-tests"
        and change_rules[0].get("status") == "passed"
    ):
        raise SystemExit("installed wheel CLI change check emitted the wrong report contract")

    lint_help = run_bounded(
        [str(executable), "workflow", "lint", "--help"],
        cwd=consumer,
        env=child_environment,
    )
    if lint_help.returncode != 0 or lint_help.stderr:
        raise SystemExit("installed wheel CLI workflow lint help failed")
    try:
        lint_help_text = lint_help.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SystemExit("installed wheel CLI workflow lint help was not UTF-8") from error
    if "pinned actionlint container" not in lint_help_text:
        raise SystemExit("installed wheel CLI workflow lint help omitted its runtime contract")

    completed = run_bounded(
        [
            str(executable),
            "commit",
            "check",
            "--message",
            SMOKE_MESSAGE,
            "--repo",
            str(consumer),
            "--config",
            str(config),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    document = successful_json(completed, operation="commit check")
    commits = document.get("commits") if isinstance(document, dict) else None
    if not (
        isinstance(document, dict)
        and document.get("schema_version") == 1
        and document.get("valid") is True
        and document.get("checked") == 1
        and document.get("passed") == 1
        and document.get("failed") == 0
        and document.get("config_path") == str(config.resolve())
        and isinstance(commits, list)
        and len(commits) == 1
        and isinstance(commits[0], dict)
        and commits[0].get("header") == SMOKE_MESSAGE
        and commits[0].get("status") == "passed"
    ):
        raise SystemExit("installed wheel CLI emitted the wrong report contract")


def main() -> int:
    """Require one wheel and sdist containing both supported execution surfaces."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Retain the validated release artifacts")
    args = parser.parse_args()
    if args.output_dir is not None and args.output_dir.exists():
        raise SystemExit("release output directory must not already exist")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required for the package build gate")
    with tempfile.TemporaryDirectory(prefix="yaga-build-") as temporary_directory:
        output = Path(temporary_directory)
        subprocess.run(
            [
                uv,
                "build",
                "--no-build-isolation",
                "--out-dir",
                str(output),
            ],
            cwd=ROOT,
            check=True,
        )
        wheels = list(output.glob("*.whl"))
        source_distributions = list(output.glob("*.tar.gz"))
        if len(wheels) != 1 or len(source_distributions) != 1:
            raise SystemExit("build must emit exactly one wheel and one source distribution")
        if not wheels[0].name.startswith("yaga_cli-"):
            raise SystemExit("wheel has the wrong distribution name")
        with zipfile.ZipFile(wheels[0]) as wheel:
            names = set(wheel.namelist())
            required = {
                "yaga/__init__.py",
                "yaga/action_cli.py",
                "yaga/commit_action_cli.py",
                "yaga/commit_action_runtime.py",
                "yaga/cli.py",
                "yaga/agent_review/runtime.py",
                "yaga/agent_review/github/runtime.py",
                "yaga/git/__init__.py",
                "yaga/git/changes.py",
                "yaga/git/process.py",
                "yaga/git/runtime.py",
                "yaga/git/tree.py",
                "yaga/commands/branch.py",
                "yaga/commands/change.py",
                "yaga/commands/commit.py",
                "yaga/commands/github.py",
                "yaga/commands/pull_request.py",
                "yaga/commands/mode.py",
                "yaga/commands/repo.py",
                "yaga/commands/path.py",
                "yaga/commands/size.py",
                "yaga/commands/test.py",
                "yaga/commands/tree.py",
                "yaga/commands/workflow.py",
                "yaga/commits/checker.py",
                "yaga/commits/dependabot.py",
                "yaga/commits/github_signature.py",
                "yaga/commits/github_event.py",
                "yaga/commits/github_reporting.py",
                "yaga/commits/service.py",
                "yaga/branches/__init__.py",
                "yaga/branches/checker.py",
                "yaga/branches/models.py",
                "yaga/branches/patterns.py",
                "yaga/branches/policy.py",
                "yaga/branches/reporting.py",
                "yaga/branches/service.py",
                "yaga/descriptions/__init__.py",
                "yaga/descriptions/checker.py",
                "yaga/descriptions/github.py",
                "yaga/descriptions/hygiene.py",
                "yaga/descriptions/markdown.py",
                "yaga/descriptions/models.py",
                "yaga/descriptions/policy.py",
                "yaga/descriptions/reporting.py",
                "yaga/descriptions/service.py",
                "yaga/descriptions/sources.py",
                "yaga/changes/__init__.py",
                "yaga/changes/checker.py",
                "yaga/changes/git.py",
                "yaga/changes/models.py",
                "yaga/changes/patterns.py",
                "yaga/changes/policy.py",
                "yaga/changes/reporting.py",
                "yaga/changes/service.py",
                "yaga/trees/__init__.py",
                "yaga/trees/checker.py",
                "yaga/trees/git.py",
                "yaga/trees/models.py",
                "yaga/trees/patterns.py",
                "yaga/trees/policy.py",
                "yaga/trees/reporting.py",
                "yaga/trees/service.py",
                "yaga/modes/__init__.py",
                "yaga/modes/checker.py",
                "yaga/modes/git.py",
                "yaga/modes/models.py",
                "yaga/modes/patterns.py",
                "yaga/modes/policy.py",
                "yaga/modes/reporting.py",
                "yaga/modes/service.py",
                "yaga/sizes/__init__.py",
                "yaga/sizes/checker.py",
                "yaga/sizes/git.py",
                "yaga/sizes/models.py",
                "yaga/sizes/patterns.py",
                "yaga/sizes/policy.py",
                "yaga/sizes/reporting.py",
                "yaga/sizes/service.py",
                "yaga/paths/__init__.py",
                "yaga/paths/checker.py",
                "yaga/paths/git.py",
                "yaga/paths/models.py",
                "yaga/paths/policy.py",
                "yaga/paths/reporting.py",
                "yaga/paths/service.py",
                "yaga/files.py",
                "yaga/path_patterns.py",
                "yaga/test_planning/__init__.py",
                "yaga/test_planning/diagnostics.py",
                "yaga/test_planning/evaluation.py",
                "yaga/test_planning/evaluation_service.py",
                "yaga/test_planning/git.py",
                "yaga/test_planning/handoff.py",
                "yaga/test_planning/limits.py",
                "yaga/test_planning/models.py",
                "yaga/test_planning/planner.py",
                "yaga/test_planning/policy.py",
                "yaga/test_planning/reporting.py",
                "yaga/test_planning/reports.py",
                "yaga/test_planning/results.py",
                "yaga/test_planning/selection.py",
                "yaga/test_planning/service.py",
                "yaga/test_planning/source.py",
                "yaga/repository/checker.py",
                "yaga/repository/models.py",
                "yaga/repository/plan.py",
                "yaga/repository/reporting.py",
                "yaga/workflows/actionlint_runtime.py",
                "yaga/workflows/actionlint_snapshot.py",
                "yaga/workflows/checker.py",
                "yaga/workflows/inputs.py",
                "yaga/workflows/lint.py",
                "yaga/workflows/parser.py",
                "yaga/workflows/security.py",
                "yaga/workflows/security_facts.py",
                "yaga/workflows/security_models.py",
                "yaga/workflows/security_reporting.py",
                "yaga/workflows/security_rules.py",
                "yaga/workflows/yaml.py",
            }
            if missing := sorted(required - names):
                raise SystemExit(f"wheel is missing required modules: {', '.join(missing)}")
            entry_points = next(
                (name for name in names if name.endswith(".dist-info/entry_points.txt")),
                None,
            )
            if entry_points is None:
                raise SystemExit("wheel does not contain console-script metadata")
            entry_point_text = wheel.read(entry_points).decode("utf-8")
            if "yaga = yaga.cli:main" not in entry_point_text:
                raise SystemExit("wheel does not expose the yaga console script")
            metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_paths) != 1:
                raise SystemExit("wheel must contain exactly one metadata document")
            validate_wheel_metadata(wheel.read(metadata_paths[0]))
        with tarfile.open(source_distributions[0], mode="r:gz") as source_distribution:
            validate_sdist_metadata(source_distribution)
            names = source_distribution.getnames()
            required_suffixes = {
                "/action.yml": "the composite Action",
                "/actions/commit-check/action.yml": "the commit-check Action",
                "/.github/workflows/commit-policy.yml": "the commit-policy dogfood workflow",
                "/.pre-commit-hooks.yaml": "the pre-commit provider manifest",
                "/examples/agent-review.yml": "the Agent review workflow example",
                "/examples/commit-policy.yml": "the commit-policy workflow example",
                "/examples/review-policy.yml": "the review policy workflow example",
                "/src/yaga/__init__.py": "the yaga package",
            }
            for suffix, label in required_suffixes.items():
                if not any(name.endswith(suffix) for name in names):
                    raise SystemExit(f"source distribution does not contain {label}")
        exercise_installed_wheel(uv, output, wheels[0])
        if args.output_dir is not None:
            args.output_dir.mkdir(parents=True)
            for artifact in (wheels[0], source_distributions[0]):
                shutil.copyfile(artifact, args.output_dir / artifact.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
