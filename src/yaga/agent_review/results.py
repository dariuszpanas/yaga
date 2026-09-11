"""Strict, provider-neutral result documents for named Agent review lenses."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from yaga.agent_review.evaluation import LensOutcome, ReviewAggregate, evaluate_policy
from yaga.agent_review.plan import build_plan
from yaga.agent_review.policy import AgentReviewPolicy
from yaga.errors import ConfigurationError, safe_error_text
from yaga.files import read_file_prefix

MAX_RESULT_BYTES = 1024 * 1024
MAX_RESULTS = 32
MAX_SUMMARY_BYTES = 4096
MAX_GITHUB_TITLE = 200
MAX_GITHUB_MESSAGE = 500
_ROOT_KEYS = frozenset({"version", "plan_digest", "results"})
_RESULT_KEYS = frozenset({"lens", "outcome", "summary"})
_LENS_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PLAN_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class LensResult:
    """One adapter result for one configured lens."""

    lens: str
    outcome: LensOutcome
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class AgentReviewResults:
    """A validated, bounded result document returned by an adapter."""

    version: int
    plan_digest: str
    results: tuple[LensResult, ...]

    def outcomes(self) -> dict[str, LensOutcome]:
        """Return the named outcomes expected by the policy evaluator."""
        return {result.lens: result.outcome for result in self.results}


def load_results(path: Path) -> AgentReviewResults:
    """Load one explicit JSON result document without contacting a provider."""
    resolved = path.expanduser().resolve()
    try:
        raw = read_file_prefix(resolved, maximum=MAX_RESULT_BYTES)
    except OSError as error:
        raise ConfigurationError(f"cannot read Agent review results {resolved}: {error}") from error
    if len(raw) > MAX_RESULT_BYTES:
        raise ConfigurationError(
            f"Agent review results exceed {MAX_RESULT_BYTES} bytes: {resolved}"
        )
    try:
        document = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ConfigurationError(f"Agent review results are not valid JSON: {resolved}") from error
    return _parse_document(document, resolved)


def evaluate_results(
    policy: AgentReviewPolicy,
    results: AgentReviewResults,
) -> ReviewAggregate:
    """Apply a validated policy to one validated adapter result document."""
    if not isinstance(policy, AgentReviewPolicy):
        raise TypeError("policy must be an AgentReviewPolicy")
    if not isinstance(results, AgentReviewResults):
        raise TypeError("results must be AgentReviewResults")
    expected_digest = build_plan(policy).digest()
    if results.plan_digest != expected_digest:
        raise ConfigurationError("Agent review results do not match the current policy plan")
    returned = set(results.outcomes())
    missing = tuple(lens.name for lens in policy.lenses if lens.name not in returned)
    if missing:
        raise ConfigurationError("Agent review results are missing lens(es): " + ", ".join(missing))
    return evaluate_policy(policy, results.outcomes())


def render_evaluation(
    results: AgentReviewResults,
    aggregate: ReviewAggregate,
    output_format: str = "text",
) -> str:
    """Render result details and aggregate state for humans or automation."""
    if not isinstance(results, AgentReviewResults):
        raise TypeError("results must be AgentReviewResults")
    if not isinstance(aggregate, ReviewAggregate):
        raise TypeError("aggregate must be a ReviewAggregate")
    document = {
        "version": results.version,
        "plan_digest": results.plan_digest,
        "state": aggregate.state.value,
        "blocking_failures": list(aggregate.blocking_failures),
        "blocking_pending": list(aggregate.blocking_pending),
        "advisory_failures": list(aggregate.advisory_failures),
        "advisory_pending": list(aggregate.advisory_pending),
        "results": [
            asdict(result) | {"outcome": result.outcome.value} for result in results.results
        ],
    }
    if output_format == "json":
        return json.dumps(document, ensure_ascii=False, indent=2)
    if output_format == "github":
        return _render_github_evaluation(results, aggregate)
    if output_format != "text":
        raise ValueError("output format must be text, json, or github")
    lines = [
        f"Agent review result: {aggregate.state.value}.",
        f"Plan digest: {results.plan_digest}",
    ]
    for result in results.results:
        detail = "" if result.summary is None else f": {safe_error_text(result.summary)}"
        lines.append(f"- {result.lens}: {result.outcome.value}{detail}")
    if aggregate.blocking_failures:
        lines.append(f"Blocking failures: {', '.join(aggregate.blocking_failures)}")
    if aggregate.blocking_pending:
        lines.append(f"Blocking pending: {', '.join(aggregate.blocking_pending)}")
    if aggregate.advisory_failures:
        lines.append(f"Advisory failures: {', '.join(aggregate.advisory_failures)}")
    if aggregate.advisory_pending:
        lines.append(f"Advisory pending: {', '.join(aggregate.advisory_pending)}")
    return "\n".join(lines)


def _render_github_evaluation(results: AgentReviewResults, aggregate: ReviewAggregate) -> str:
    """Render bounded workflow commands for adapter-oriented CI jobs."""
    by_lens = {result.lens: result for result in results.results}
    lines: list[str] = []
    for lens in aggregate.blocking_failures:
        lines.append(_github_annotation(by_lens.get(lens), lens, "error"))
    for lens in aggregate.blocking_pending:
        lines.append(_github_annotation(by_lens.get(lens), lens, "error"))
    for lens in aggregate.advisory_failures:
        lines.append(_github_annotation(by_lens.get(lens), lens, "warning"))
    for lens in aggregate.advisory_pending:
        lines.append(_github_annotation(by_lens.get(lens), lens, "warning"))
    summary = (
        f"Agent review result: {aggregate.state.value}; "
        f"{len(aggregate.blocking_failures)} blocking failure(s), "
        f"{len(aggregate.blocking_pending)} blocking pending, "
        f"{len(aggregate.advisory_failures)} advisory failure(s), "
        f"{len(aggregate.advisory_pending)} advisory pending; "
        f"plan {results.plan_digest}."
    )
    lines.append(
        f"::notice title={_github_property('YAGA Agent review', maximum=MAX_GITHUB_TITLE)}::"
        f"{_github_data(summary, maximum=MAX_GITHUB_MESSAGE)}"
    )
    return "\n".join(lines)


def _github_annotation(result: LensResult | None, lens: str, level: str) -> str:
    """Render one escaped failure or pending lens annotation."""
    outcome = "pending" if result is None else result.outcome.value
    detail = (
        "no result was returned" if result is None or result.summary is None else result.summary
    )
    message = f"{outcome}: {detail}"
    title = _github_property(f"YAGA Agent review: {lens}", maximum=MAX_GITHUB_TITLE)
    return f"::{level} title={title}::{_github_data(message, maximum=MAX_GITHUB_MESSAGE)}"


def _github_property(value: str, *, maximum: int) -> str:
    escaped = _github_escape(value).replace(":", "%3A").replace(",", "%2C")
    return _bounded_github_escape(escaped, maximum=maximum)


def _github_data(value: str, *, maximum: int) -> str:
    return _bounded_github_escape(_github_escape(value), maximum=maximum)


def _github_escape(value: str) -> str:
    cleaned = "".join(
        "?"
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"} and character not in {"\r", "\n"}
        else character
        for character in value
    )
    return cleaned.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _bounded_github_escape(value: str, *, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    cutoff = maximum - 1
    last_escape = value.rfind("%", 0, cutoff)
    if last_escape >= 0 and cutoff - last_escape < 3:
        cutoff = last_escape
    return f"{value[:cutoff]}…"


def _parse_document(document: Any, path: Path) -> AgentReviewResults:
    if not isinstance(document, dict):
        raise ConfigurationError(f"Agent review results must be an object in {path}")
    _reject_unknown(document, _ROOT_KEYS, "Agent review results", path)
    version = document.get("version")
    if type(version) is not int or version != 1:
        raise ConfigurationError(f"Agent review results.version must be integer 1 in {path}")
    plan_digest = document.get("plan_digest")
    if not isinstance(plan_digest, str) or not _PLAN_DIGEST.fullmatch(plan_digest):
        raise ConfigurationError(f"Agent review results.plan_digest is invalid in {path}")
    raw_results = document.get("results")
    if not isinstance(raw_results, list) or len(raw_results) > MAX_RESULTS:
        raise ConfigurationError(f"Agent review results.results is unbounded or invalid in {path}")
    parsed: list[LensResult] = []
    seen: set[str] = set()
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            raise ConfigurationError(f"Agent review result entries must be objects in {path}")
        _reject_unknown(raw_result, _RESULT_KEYS, "Agent review result", path)
        lens = raw_result.get("lens")
        if not isinstance(lens, str) or not _LENS_NAME.fullmatch(lens) or lens in seen:
            raise ConfigurationError(f"Agent review result lens is invalid or repeated in {path}")
        seen.add(lens)
        try:
            outcome = LensOutcome(raw_result.get("outcome"))
        except (TypeError, ValueError) as error:
            raise ConfigurationError(f"Agent review result outcome is invalid in {path}") from error
        summary = raw_result.get("summary")
        if summary is not None and (
            not isinstance(summary, str)
            or not summary.strip()
            or "\x00" in summary
            or len(summary.encode("utf-8")) > MAX_SUMMARY_BYTES
        ):
            raise ConfigurationError(f"Agent review result summary is invalid in {path}")
        parsed.append(LensResult(lens, outcome, summary))
    return AgentReviewResults(version, plan_digest, tuple(parsed))


def _reject_unknown(
    mapping: dict[str, Any], allowed: frozenset[str], label: str, path: Path
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ConfigurationError(f"{label} has unknown key {unknown[0]} in {path}")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(value)
