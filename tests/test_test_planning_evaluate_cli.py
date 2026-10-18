"""Explicit, bounded installed receipt evaluation without Git or execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from yaga.cli import app
from yaga.commands import test as test_commands
from yaga.errors import InputError
from yaga.test_planning import evaluation_service
from yaga.test_planning.evaluation_service import load_evaluation_json
from yaga.test_planning.models import (
    Profile,
    QualificationPolicy,
    SelectionMode,
    SelectionRule,
    Suite,
)
from yaga.test_planning.planner import plan_tests
from yaga.test_planning.reporting import test_plan_document as plan_document
from yaga.test_planning.reports import PlannedTests
from yaga.test_planning.source import PlanSource

runner = CliRunner()
POLICY_SHA = "a" * 64
BINDINGS_SHA = "b" * 64
COMMIT = "2" * 40
TREE = "3" * 40
RUN_ID = "ci-101"
EXPECTED = {
    "--policy-sha256": POLICY_SHA,
    "--bindings-sha256": BINDINGS_SHA,
    "--execution-commit": COMMIT,
    "--execution-tree": TREE,
    "--run-id": RUN_ID,
    "--attempt": "1",
}


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    policy = QualificationPolicy(
        1,
        suites=(
            Suite("backend", "pytest", variants=("py312", "py315"), needs=("build",)),
            Suite("build", "docker"),
            Suite("frontend", "vitest"),
        ),
        profiles=(Profile("development", SelectionMode.SELECTIVE),),
        rules=(SelectionRule("backend", ("src/**",), select=("backend",)),),
    )
    planned = PlannedTests(
        POLICY_SHA,
        BINDINGS_SHA,
        PlanSource("1" * 40, COMMIT, "1" * 40, COMMIT, TREE),
        plan_tests(policy, profile="development", changed_paths=("src/worker.py",)),
    )
    plan = plan_document(planned)
    results = {
        "test-results-version": 1,
        "plan_digest": plan["plan_digest"],
        "policy_sha256": POLICY_SHA,
        "bindings_sha256": BINDINGS_SHA,
        "execution_commit": COMMIT,
        "execution_tree": TREE,
        "run_id": RUN_ID,
        "attempt": 1,
        "results": [
            {"suite": "backend", "variant": "py312", "binding": "pytest", "outcome": "passed"},
            {"suite": "backend", "variant": "py315", "binding": "pytest", "outcome": "passed"},
            {"suite": "build", "variant": "default", "binding": "docker", "outcome": "passed"},
        ],
    }
    plan_path, results_path = tmp_path / "plan.json", tmp_path / "results.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    results_path.write_text(json.dumps(results), encoding="utf-8")
    return plan_path, results_path, results


def arguments(inputs: tuple[Path, Path, dict[str, Any]]) -> list[str]:
    plan_path, results_path, _ = inputs
    return [
        "test",
        "evaluate",
        "--plan",
        str(plan_path),
        "--results",
        str(results_path),
        *(value for pair in EXPECTED.items() for value in pair),
        "--format",
        "json",
    ]


def assert_error(result: Result) -> dict[str, Any]:
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    document = json.loads(result.stderr)
    assert document["kind"] == "test_evaluation"
    assert document["status"] == "error"
    assert document["valid"] is False
    return document


def write_results(inputs: tuple[Path, Path, dict[str, Any]], document: object) -> None:
    inputs[1].write_text(json.dumps(document), encoding="utf-8")


def test_evaluate_help_requires_explicit_files_and_independent_identities() -> None:
    result = runner.invoke(app, ["test", "evaluate", "--help"])
    assert result.exit_code == 0
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stdout)
    for option in ("--plan", "--results", *EXPECTED, "--format", "--quiet"):
        assert option in text
    for unsupported in ("--repo", "--range", "--revision", "--execute", "--token"):
        assert unsupported not in text


@pytest.mark.parametrize("option", ["--plan", "--results", *EXPECTED])
def test_evaluate_has_no_defaults_for_inputs_or_expected_identities(
    inputs: tuple[Path, Path, dict[str, Any]], option: str
) -> None:
    args = arguments(inputs)
    index = args.index(option)
    del args[index : index + 2]
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert option in re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stderr)


def test_evaluate_passes_without_git_and_reads_only_the_two_explicit_files(
    inputs: tuple[Path, Path, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    reads = []
    original = evaluation_service.read_regular_file_prefix

    def read(path: Path, *, maximum: int) -> bytes:
        reads.append(path)
        assert maximum == 4 * 1024 * 1024
        return original(path, maximum=maximum)

    monkeypatch.setattr(evaluation_service, "read_regular_file_prefix", read)
    monkeypatch.setattr(test_commands, "create_test_plan", lambda *a, **k: pytest.fail("Git used"))
    result = runner.invoke(app, arguments(inputs))
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["test-evaluation-version"] == 1
    assert document["status"] == "passed"
    assert document["passed"] is document["complete"] is True
    assert reads == [inputs[0].resolve(), inputs[1].resolve()]


def test_evaluate_text_and_quiet_success(inputs: tuple[Path, Path, dict[str, Any]]) -> None:
    args = arguments(inputs)
    args[-1] = "text"
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert "Test evaluation: passed" in result.stdout
    assert "no tests were executed" in result.stdout
    quiet = runner.invoke(app, [*args, "--quiet"])
    assert quiet.exit_code == 0
    assert quiet.stdout == quiet.stderr == ""


@pytest.mark.parametrize("output_format", ["json", "text"])
@pytest.mark.parametrize(
    "attempt", ["", "0", "-1", "2147483648", "1.0", "true", "\u0661", "x" * 20_000, "9" * 20_000]
)
def test_evaluate_invalid_attempt_has_bounded_error_before_file_reads(
    inputs: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    attempt: str,
    output_format: str,
) -> None:
    monkeypatch.setattr(
        test_commands, "load_evaluation_json", lambda *a: pytest.fail("unexpected file read")
    )
    args = arguments(inputs)
    args[args.index("--attempt") + 1] = attempt
    args[-1] = output_format
    result = runner.invoke(app, [*args, "--quiet"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert len(result.stderr.encode("utf-8")) < 1_000
    if output_format == "json":
        assert "test attempt must be an integer" in assert_error(result)["error"]["message"]
    else:
        assert "test attempt must be an integer" in result.stderr


def test_evaluate_accepts_maximum_attempt(inputs: tuple[Path, Path, dict[str, Any]]) -> None:
    inputs[2]["attempt"] = 2_147_483_647
    write_results(inputs, inputs[2])
    args = arguments(inputs)
    args[args.index("--attempt") + 1] = "2147483647"
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["attempt"] == 2_147_483_647


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        ("failed", "failed"),
        ("cancelled", "failed"),
        ("pending", "incomplete"),
        ("blocked", "incomplete"),
    ],
)
@pytest.mark.parametrize("quiet", [False, True])
def test_evaluate_nonpassing_receipts_remain_visible(
    inputs: tuple[Path, Path, dict[str, Any]], outcome: str, status: str, quiet: bool
) -> None:
    receipt = inputs[2]
    receipt["results"][0]["outcome"] = outcome
    write_results(inputs, receipt)
    result = runner.invoke(app, [*arguments(inputs), *(["--quiet"] if quiet else [])])
    assert result.exit_code == 1, result.output
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["status"] == status
    assert document["passed"] is False


def test_evaluate_missing_prerequisite_cell_is_incomplete_and_not_quiet(
    inputs: tuple[Path, Path, dict[str, Any]],
) -> None:
    receipt = inputs[2]
    receipt["results"].pop()
    write_results(inputs, receipt)
    args = arguments(inputs)
    args[-1] = "text"
    result = runner.invoke(app, [*args, "--quiet"])
    assert result.exit_code == 1
    assert result.stderr == ""
    assert "Test evaluation: incomplete" in result.stdout
    assert "test.required-result" in result.stdout
    assert "build/default" in result.stdout


@pytest.mark.parametrize(
    ("field", "value"),
    [("outcome", "skipped"), ("suite", "unknown"), ("variant", "unknown"), ("binding", "other")],
)
def test_evaluate_rejects_invalid_or_unexpected_cells(
    inputs: tuple[Path, Path, dict[str, Any]], field: str, value: str
) -> None:
    receipt = inputs[2]
    receipt["results"][0][field] = value
    write_results(inputs, receipt)
    assert_error(runner.invoke(app, arguments(inputs)))


def test_evaluate_rejects_duplicate_and_unselected_cells(
    inputs: tuple[Path, Path, dict[str, Any]],
) -> None:
    receipt = deepcopy(inputs[2])
    receipt["results"].append(receipt["results"][0])
    write_results(inputs, receipt)
    assert_error(runner.invoke(app, arguments(inputs)))
    receipt["results"][-1] = {
        "suite": "frontend",
        "variant": "default",
        "binding": "vitest",
        "outcome": "passed",
    }
    write_results(inputs, receipt)
    assert_error(runner.invoke(app, arguments(inputs)))


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--policy-sha256", "c" * 64),
        ("--bindings-sha256", "c" * 64),
        ("--execution-commit", "4" * 40),
        ("--execution-tree", "4" * 40),
        ("--run-id", "different-run"),
        ("--attempt", "2"),
        ("--policy-sha256", "invalid"),
        ("--run-id", "bad run"),
        ("--attempt", "0"),
    ],
)
def test_evaluate_requires_independent_exact_identities(
    inputs: tuple[Path, Path, dict[str, Any]], option: str, value: str
) -> None:
    args = arguments(inputs)
    args[args.index(option) + 1] = value
    assert_error(runner.invoke(app, [*args, "--quiet"]))


@pytest.mark.parametrize(
    "field",
    [
        "plan_digest",
        "policy_sha256",
        "bindings_sha256",
        "execution_commit",
        "execution_tree",
        "run_id",
        "attempt",
    ],
)
def test_evaluate_rejects_displaced_result_identities(
    inputs: tuple[Path, Path, dict[str, Any]], field: str
) -> None:
    receipt = inputs[2]
    original = receipt[field]
    receipt[field] = (
        2 if field == "attempt" else "other-run" if field == "run_id" else "c" * len(original)
    )
    write_results(inputs, receipt)
    assert_error(runner.invoke(app, arguments(inputs)))


@pytest.mark.parametrize("input_index", [0, 1])
@pytest.mark.parametrize(
    "raw",
    [
        b'{"key":1,"key":2}',
        b'{"nested":{"key":1,"key":2}}',
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"1e999",
        b"\xff",
        b"{} trailing",
        b"[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]",
    ],
)
def test_evaluate_rejects_malformed_json_at_either_boundary(
    inputs: tuple[Path, Path, dict[str, Any]], input_index: int, raw: bytes
) -> None:
    inputs[:2][input_index].write_bytes(raw)
    assert_error(runner.invoke(app, arguments(inputs)))


@pytest.mark.parametrize("input_index", [0, 1])
def test_evaluate_rejects_missing_directory_and_oversized_files(
    inputs: tuple[Path, Path, dict[str, Any]], input_index: int
) -> None:
    path = inputs[:2][input_index]
    path.unlink()
    assert_error(runner.invoke(app, arguments(inputs)))
    path.mkdir()
    assert_error(runner.invoke(app, arguments(inputs)))
    path.rmdir()
    path.write_bytes(b" " * (evaluation_service.MAX_INPUT_BYTES + 1))
    assert_error(runner.invoke(app, arguments(inputs)))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires a POSIX FIFO")
def test_evaluate_rejects_fifo_without_waiting(
    inputs: tuple[Path, Path, dict[str, Any]],
) -> None:
    inputs[1].unlink()
    mkfifo = getattr(os, "mkfifo", None)
    assert mkfifo is not None
    mkfifo(inputs[1])
    assert_error(runner.invoke(app, arguments(inputs)))


def test_reader_depth_scanner_ignores_escaped_quotes_and_brackets(tmp_path: Path) -> None:
    path = tmp_path / "quoted.json"
    value = {"quoted": '["' * 100 + '\\"' * 100}
    path.write_text(json.dumps(value), encoding="utf-8")
    assert load_evaluation_json(path) == value


def test_reader_exact_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "input.json"
    raw = b'{"value":0}'
    path.write_bytes(raw)
    monkeypatch.setattr(evaluation_service, "MAX_INPUT_BYTES", len(raw))
    assert load_evaluation_json(path) == {"value": 0}
    monkeypatch.setattr(evaluation_service, "MAX_INPUT_BYTES", len(raw) - 1)
    with pytest.raises(InputError, match="byte limit"):
        load_evaluation_json(path)


def test_evaluate_errors_are_bounded_sanitized_and_visible_with_quiet(
    inputs: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_path: Path) -> object:
        raise InputError("\x1b[31m unsafe\n\u202e" + "x" * 4_000)

    monkeypatch.setattr(test_commands, "load_evaluation_json", fail)
    result = runner.invoke(app, [*arguments(inputs), "--quiet"])
    document = assert_error(result)
    assert len(document["error"]["message"]) <= 1_000
    assert "\x1b" not in document["error"]["message"]
    assert "\u202e" not in document["error"]["message"]
    assert "\n" not in document["error"]["message"]


def test_evaluate_quiet_still_checks_complete_output_bound(
    inputs: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def oversized(_report: object) -> bytes:
        raise InputError("test evaluation report exceeds its output byte limit")

    monkeypatch.setattr(test_commands, "encode_evaluation", oversized)
    assert_error(runner.invoke(app, [*arguments(inputs), "--quiet"]))


def test_evaluate_reports_exact_omitted_diagnostics_for_large_catalog(
    inputs: tuple[Path, Path, dict[str, Any]],
) -> None:
    policy = QualificationPolicy(
        1,
        suites=tuple(
            Suite(f"suite{index}", "pytest", variants=tuple(f"variant{item}" for item in range(16)))
            for index in range(32)
        ),
        profiles=(Profile("full", SelectionMode.FULL),),
    )
    planned = PlannedTests(
        POLICY_SHA,
        BINDINGS_SHA,
        PlanSource(None, None, None, COMMIT, TREE),
        plan_tests(policy, profile="full", changed_paths=None),
    )
    document = plan_document(planned)
    inputs[0].write_text(json.dumps(document), encoding="utf-8")
    results = inputs[2]
    results["plan_digest"] = document["plan_digest"]
    results["results"] = []
    write_results(inputs, results)
    args = arguments(inputs)
    args[-1] = "text"
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert result.stderr == ""
    assert "Diagnostics: 512" in result.stdout
    assert result.stdout.count("[test.required-result]") == 256
    assert "256 additional diagnostic(s) omitted" in result.stdout


def test_reader_rejects_long_numeric_literals_before_conversion(tmp_path: Path) -> None:
    path = tmp_path / "number.json"
    for raw in (b"1" * 1000, b"0." + b"1" * 1000):
        path.write_bytes(raw)
        with pytest.raises(InputError, match="bounded UTF-8 JSON"):
            load_evaluation_json(path)


def test_evaluate_preview_plan_and_tampered_plan_digest_cannot_pass(
    inputs: tuple[Path, Path, dict[str, Any]],
) -> None:
    document = json.loads(inputs[0].read_bytes())
    document["plan"]["bindings_sha256"] = None
    inputs[0].write_text(json.dumps(document), encoding="utf-8")
    assert_error(runner.invoke(app, arguments(inputs)))
    document["plan_digest"] = hashlib.sha256(
        json.dumps(document["plan"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    inputs[0].write_text(json.dumps(document), encoding="utf-8")
    results = inputs[2]
    results["plan_digest"] = document["plan_digest"]
    write_results(inputs, results)
    assert_error(runner.invoke(app, arguments(inputs)))
