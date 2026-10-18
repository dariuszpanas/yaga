"""Strict, immutable result receipts for externally executed test-plan cells.

Receipts correlate an adapter's declarations. They do not authenticate jobs,
artifacts, custom assertions, environment identity, or cleanup by themselves.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from yaga.errors import ConfigurationError, InputError
from yaga.test_planning.limits import MAX_CELLS, MAX_PLAN_BYTES, MAX_TOKEN_BYTES
from yaga.test_planning.models import Suite
from yaga.test_planning.source import PlanSource

TEST_RESULTS_VERSION = 1
MAX_SUMMARY_BYTES = 1_000
MAX_ATTEMPT = 2_147_483_647
_DIGEST = re.compile(r"[0-9a-f]{64}", flags=re.ASCII)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", flags=re.ASCII)
_RESULT_FIELDS = {"suite", "variant", "binding", "outcome"}
_OPTIONAL_RESULT_FIELDS = {"summary", "evidence_sha256"}
_ENVELOPE_FIELDS = {
    "test-results-version",
    "plan_digest",
    "policy_sha256",
    "bindings_sha256",
    "execution_commit",
    "execution_tree",
    "run_id",
    "attempt",
    "results",
}


class ResultOutcome(StrEnum):
    """The closed adapter-declared outcomes; a skipped test cannot pass."""

    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class CellResult:
    """One complete suite/variant declaration from the authorized aggregate attempt."""

    suite: str
    variant: str
    binding: str
    outcome: ResultOutcome
    summary: str | None = None
    evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        _validate_cell(self)


@dataclass(frozen=True, slots=True)
class TestResults:
    """One source-bound envelope; cells may be absent but cannot be duplicated."""

    plan_digest: str
    policy_sha256: str
    bindings_sha256: str
    execution_commit: str
    execution_tree: str
    run_id: str
    attempt: int
    results: tuple[CellResult, ...]
    test_results_version: int = TEST_RESULTS_VERSION

    def __post_init__(self) -> None:
        _validate_results(self)
        _encode_document(_document(self))


def parse_results_document(value: object) -> TestResults:
    """Parse a closed v1 decoded JSON object without reading files or CI state.

    The caller must bound the original JSON read and reject duplicate object
    keys. Optional cell fields may be omitted; explicit nulls are invalid.
    Result order is immaterial, and canonical output sorts by suite and variant.
    """
    envelope = _object(value, _ENVELOPE_FIELDS)
    raw_results = envelope["results"]
    if type(raw_results) is not list or len(raw_results) > MAX_CELLS:
        raise InputError("test results require a bounded JSON array of cells")
    try:
        return TestResults(
            plan_digest=_string(envelope["plan_digest"], maximum=64),
            policy_sha256=_string(envelope["policy_sha256"], maximum=64),
            bindings_sha256=_string(envelope["bindings_sha256"], maximum=64),
            execution_commit=_string(envelope["execution_commit"], maximum=64),
            execution_tree=_string(envelope["execution_tree"], maximum=64),
            run_id=_string(envelope["run_id"], maximum=128),
            attempt=envelope["attempt"],
            results=tuple(_parse_cell(item) for item in raw_results),
            test_results_version=envelope["test-results-version"],
        )
    except (TypeError, ValueError) as error:
        raise InputError("invalid version-one test result semantics") from error


def results_document(results: TestResults) -> dict[str, Any]:
    """Return complete canonical v1 receipt content, without authenticating it."""
    if type(results) is not TestResults:
        raise InputError("result encoding requires a TestResults model")
    try:
        _validate_results(results)
    except (TypeError, ValueError) as error:
        raise InputError("result encoding requires a valid TestResults model") from error
    document = _document(results)
    _encode_document(document)
    return document


def encode_results(results: TestResults) -> bytes:
    """Encode complete canonical UTF-8 JSON, including a bounded final newline."""
    return _encode_document(results_document(results))


def _document(results: TestResults) -> dict[str, Any]:
    return {
        "test-results-version": TEST_RESULTS_VERSION,
        "plan_digest": results.plan_digest,
        "policy_sha256": results.policy_sha256,
        "bindings_sha256": results.bindings_sha256,
        "execution_commit": results.execution_commit,
        "execution_tree": results.execution_tree,
        "run_id": results.run_id,
        "attempt": results.attempt,
        "results": [
            {
                "suite": result.suite,
                "variant": result.variant,
                "binding": result.binding,
                "outcome": result.outcome.value,
                **({"summary": result.summary} if result.summary is not None else {}),
                **(
                    {"evidence_sha256": result.evidence_sha256}
                    if result.evidence_sha256 is not None
                    else {}
                ),
            }
            for result in sorted(results.results, key=lambda item: (item.suite, item.variant))
        ],
    }


def _validate_cell(result: CellResult) -> None:
    Suite(result.suite, result.binding, variants=(result.variant,))
    if type(result.outcome) is not ResultOutcome:
        raise ValueError("cell outcome must be a ResultOutcome")
    if result.summary is not None:
        if type(result.summary) is not str or len(result.summary) > MAX_SUMMARY_BYTES:
            raise ValueError("cell summary exceeds its bounded string limit")
        if len(result.summary.encode("utf-8")) > MAX_SUMMARY_BYTES:
            raise ValueError("cell summary exceeds its UTF-8 byte limit")
    if result.evidence_sha256 is not None:
        validate_digest(result.evidence_sha256)


def _validate_results(results: TestResults) -> None:
    if (
        type(results.test_results_version) is not int
        or results.test_results_version != TEST_RESULTS_VERSION
    ):
        raise ValueError("unsupported test results version")
    for digest in (results.plan_digest, results.policy_sha256, results.bindings_sha256):
        validate_digest(digest)
    validate_execution_identity(results.execution_commit, results.execution_tree)
    validate_run_identity(results.run_id, results.attempt)
    if (
        type(results.results) is not tuple
        or len(results.results) > MAX_CELLS
        or any(type(item) is not CellResult for item in results.results)
    ):
        raise ValueError("test results require a bounded immutable tuple of CellResult models")
    cells: set[tuple[str, str]] = set()
    for result in results.results:
        _validate_cell(result)
        key = (result.suite, result.variant)
        if key in cells:
            raise ValueError("test results must not repeat a suite/variant cell")
        cells.add(key)


def validate_digest(value: object) -> None:
    """Validate one required lowercase SHA-256 identity without coercion."""
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ValueError("test result identity must be a lowercase SHA-256 digest")


def validate_execution_identity(commit: object, tree: object) -> None:
    """Validate exact same-format Git source identities without accessing Git."""
    if type(commit) is not str or type(tree) is not str:
        raise ValueError("test execution identities must be strings")
    PlanSource(None, None, None, commit, tree)


def validate_run_identity(run_id: object, attempt: object) -> None:
    """Validate the explicit provider-neutral run and aggregate attempt identity."""
    if type(run_id) is not str or not _RUN_ID.fullmatch(run_id):
        raise ValueError("test run ID must use the bounded portable run grammar")
    if type(attempt) is not int or not 1 <= attempt <= MAX_ATTEMPT:
        raise ValueError("test attempt must be an integer from 1 through 2147483647")


def _parse_cell(value: object) -> CellResult:
    cell = _object(value, _RESULT_FIELDS, optional=_OPTIONAL_RESULT_FIELDS)
    return CellResult(
        suite=_string(cell["suite"], maximum=MAX_TOKEN_BYTES),
        variant=_string(cell["variant"], maximum=MAX_TOKEN_BYTES),
        binding=_string(cell["binding"], maximum=MAX_TOKEN_BYTES),
        outcome=ResultOutcome(_string(cell["outcome"], maximum=16)),
        summary=_string(cell["summary"], maximum=MAX_SUMMARY_BYTES) if "summary" in cell else None,
        evidence_sha256=_string(cell["evidence_sha256"], maximum=64)
        if "evidence_sha256" in cell
        else None,
    )


def _object(
    value: object, required: set[str], *, optional: set[str] | None = None
) -> dict[str, Any]:
    allowed = required | (optional or set())
    if (
        type(value) is not dict
        or not len(required) <= len(value) <= len(allowed)
        or any(type(key) is not str for key in value)
        or not required <= value.keys() <= allowed
    ):
        raise InputError("test results require exactly their versioned JSON object fields")
    return cast(dict[str, Any], value)


def _string(value: object, *, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum:
        raise InputError("test result field has an invalid type or exceeds its string limit")
    return value


def _encode_document(document: dict[str, Any], *, maximum: int | None = None) -> bytes:
    """Encode a document already built from bounded validated domain values."""
    maximum = MAX_PLAN_BYTES if maximum is None else maximum
    remaining = maximum - 1
    chunks: list[bytes] = []
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    try:
        for chunk in encoder.iterencode(document):
            encoded = chunk.encode("utf-8")
            remaining -= len(encoded)
            if remaining < 0:
                raise ConfigurationError(
                    f"test result report exceeds the hard {maximum}-byte limit"
                )
            chunks.append(encoded)
    except (TypeError, ValueError) as error:
        raise InputError("test result report is not valid UTF-8 JSON") from error
    return b"".join(chunks) + b"\n"
