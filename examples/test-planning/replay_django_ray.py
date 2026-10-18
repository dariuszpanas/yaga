"""Replay explicit merged revisions through a reviewed django-ray shadow adapter.

Run from an installed YAGA environment. The consumer checkout is executable,
trusted local input; historical source is only inspected by YAGA's Git reader.
This experiment measures selection and planning overhead, never test runtime.
The consumer must remain a clean checkout of the supplied adapter commit/tree.
Ignored local environments and bytecode are allowed; this correlates source in a
trusted checkout, not execution isolation against concurrent host modification.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, cast

from yaga.errors import GitError, InputError, YagaError
from yaga.git import (
    GitRepository,
    open_repository,
    parse_object_id,
    resolve_committed_tree,
    run_git,
)
from yaga.test_planning.diagnostics import diagnose_selection, parse_plan_document

MAX_BYTES = 4 * 1024 * 1024
OID = re.compile(r"[0-9a-f]{40}", re.ASCII)
JOBS = frozenset({"application", "cancellation", "latency", "monitoring", "native"})


def verify_consumer_checkout(consumer: Path, adapter_commit: str, adapter_tree: str) -> Path:
    """Correlate a trusted, Git-clean checkout with its explicit recorded source."""
    if any(
        type(value) is not str or OID.fullmatch(value) is None
        for value in (adapter_commit, adapter_tree)
    ):
        raise InputError("adapter identities must be exact Git objects")
    repository = open_repository(consumer)
    if (
        _git_output(repository, ["rev-parse", "--is-inside-work-tree"], maximum=16) != b"true\n"
        or _git_output(repository, ["rev-parse", "--show-prefix"], maximum=4096) != b"\n"
    ):
        raise InputError("consumer must name the root of its Git checkout")
    expected = resolve_committed_tree(repository.directory, adapter_commit)
    if expected.commit_sha != adapter_commit or expected.tree_sha != adapter_tree:
        raise InputError("consumer adapter commit and tree do not match")
    checked_out = parse_object_id(
        _git_output(
            repository, ["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"], maximum=256
        ),
        label="consumer checkout",
    )
    if checked_out != adapter_commit:
        raise InputError("consumer checkout does not match the adapter commit")
    if _git_output(
        repository,
        [
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
            "--no-renames",
        ],
        maximum=MAX_BYTES,
    ):
        raise InputError("consumer checkout has tracked or untracked changes")
    return repository.directory


def _git_output(repository: GitRepository, arguments: list[str], *, maximum: int) -> bytes:
    result = run_git(repository, arguments, stdout_limit=maximum)
    if result.returncode != 0 or result.stderr:
        raise GitError("unable to verify the consumer checkout")
    return result.stdout


def encode_replay(document: dict[str, Any]) -> bytes:
    """Bound the complete retained payload, including its trailing newline."""
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    if len(encoded) > MAX_BYTES:
        raise ValueError("complete replay exceeds the output limit")
    return encoded


def validate_cohort(cohort: object) -> list[dict[str, Any]]:
    """Require complete explicit ranges, with unique PR and source identities."""
    if type(cohort) is not list or not 1 <= len(cohort) <= 100:
        raise ValueError("cohort must contain one through 100 explicit revisions")
    rows = cast(list[dict[str, Any]], cohort)
    numbers, heads = set(), set()
    for row in rows:
        if type(row) is not dict or set(row) != {"number", "merged_at", "base", "head", "tree"}:
            raise ValueError("cohort row fields are incomplete or unsupported")
        number = row["number"]
        if type(number) is not int or not 1 <= number <= 2**31 - 1 or number in numbers:
            raise ValueError("cohort requires unique positive PR numbers")
        for field in ("base", "head", "tree"):
            if type(row[field]) is not str or OID.fullmatch(row[field]) is None:
                raise ValueError("cohort requires exact Git identities")
        if row["head"] in heads or row["base"] == row["head"]:
            raise ValueError("cohort requires unique nonempty revision ranges")
        if (
            type(row["merged_at"]) is not str
            or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", row["merged_at"]
            )
            is None
        ):
            raise ValueError("cohort requires UTC merge timestamps")
        numbers.add(number)
        heads.add(row["head"])
    return rows


def replay(adapter: Any, cohort: object, **inputs: Any) -> dict[str, Any]:
    """Run a frozen current selector and policy against every exact merged range."""
    rows = validate_cohort(cohort)
    observations = []
    for row in rows:
        documents = adapter.generate(
            **inputs,
            revision_range=row["base"] + ".." + row["head"],
            execution_commit=row["head"],
            execution_tree=row["tree"],
        )
        comparison = documents["comparison.json"]
        plan = documents["proposed-plan.json"]
        observations.append(
            {
                "revision": row,
                "comparison": comparison,
                "plan": plan,
            }
        )
    times = [row["comparison"]["planning_seconds"] for row in observations]
    return {
        "experiment_version": 1,
        "repository": "dariuszpanas/django-ray",
        "mode": "historical-selection-replay",
        "activation": "disabled",
        "cohort": rows,
        "observations": observations,
        "summary": {
            "revisions": len(rows),
            "omissions": sum(bool(row["comparison"]["missing"]) for row in observations),
            "additional_selection_revisions": sum(
                bool(row["comparison"]["additional"]) for row in observations
            ),
            "full_selection_revisions": sum(
                len(row["comparison"]["planned_selected"]) == 5 for row in observations
            ),
            "empty_selection_revisions": sum(
                not row["comparison"]["planned_selected"] for row in observations
            ),
            "planning_seconds_median": statistics.median(times),
            "planning_seconds_maximum": max(times),
            "test_runtime_observations": 0,
            "feedback_time_reduction": None,
            "activation_threshold": "measure-first-decide-before-activation",
        },
    }


def replay_diagnostics(document: dict[str, Any]) -> dict[str, Any]:
    """Explain a complete replay independently of its frozen version-one envelope.

    Recorded plan reasons describe only classification the planner performed.
    A full trigger can bypass rule/unknown evaluation; absent unknown reasons do
    not prove that every changed path has an owner. Nothing here rematches paths,
    changes selection, executes historical code, or establishes failure coverage.
    """
    replay_bytes = encode_replay(document)
    if (
        type(document.get("experiment_version")) is not int
        or document["experiment_version"] != 1
        or document.get("repository") != "dariuszpanas/django-ray"
        or document.get("mode") != "historical-selection-replay"
        or document.get("activation") != "disabled"
    ):
        raise ValueError("diagnostics require an unchanged version-one historical replay")
    cohort = validate_cohort(document.get("cohort"))
    observations = document.get("observations")
    if type(observations) is not list or len(observations) != len(cohort):
        raise ValueError("diagnostics require every cohort observation exactly once")
    results = []
    identities = set()
    for revision, raw_observation in zip(cohort, observations, strict=True):
        if type(raw_observation) is not dict or set(raw_observation) != {
            "revision",
            "comparison",
            "plan",
        }:
            raise ValueError("diagnostic observations must preserve the complete cohort order")
        observation = cast(dict[str, Any], raw_observation)
        if observation["revision"] != revision:
            raise ValueError("diagnostic observations must preserve the complete cohort order")
        planned = parse_plan_document(observation["plan"])
        source = planned.source
        if (
            source.base_commit != revision["base"]
            or source.comparison_commit != revision["base"]
            or source.head_commit != revision["head"]
            or source.execution_commit != revision["head"]
            or source.execution_tree != revision["tree"]
            or {suite.id for suite in planned.selection.inventory}
            != {"qualification." + job for job in JOBS}
        ):
            raise ValueError("diagnostic plan differs from the cohort source or fixed catalog")
        comparison = observation["comparison"]
        baseline = _job_selection(comparison, "baseline_selected")
        proposed = _job_selection(comparison, "planned_selected")
        missing = _job_selection(comparison, "missing")
        additional = _job_selection(comparison, "additional")
        diagnostic = diagnose_selection(
            planned, baseline_suites=tuple("qualification." + job for job in sorted(baseline))
        )
        if (
            type(comparison.get("test-shadow-version")) is not int
            or comparison["test-shadow-version"] != 1
            or comparison.get("status") != "compared"
            or comparison.get("effective_mode") != planned.selection.effective_mode.value
            or proposed
            != {item.suite.removeprefix("qualification.") for item in planned.selection.selected}
            or missing != baseline - proposed
            or additional != proposed - baseline
            or comparison.get("plan_digest") != diagnostic["plan_digest"]
            or comparison.get("policy_sha256") != planned.policy_sha256
            or comparison.get("bindings_sha256") != planned.bindings_sha256
            or comparison.get("source") != diagnostic["source"]
            or comparison.get("activation") != "disabled"
        ):
            raise ValueError("diagnostic comparison does not agree with its exact plan")
        adapter = comparison.get("adapter_source")
        baseline_digest = comparison.get("baseline_policy_sha256")
        if (
            type(adapter) is not dict
            or set(adapter) != {"commit", "tree"}
            or any(
                type(value) is not str or OID.fullmatch(value) is None for value in adapter.values()
            )
            or type(baseline_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", baseline_digest) is None
        ):
            raise ValueError("diagnostics require exact adapter and baseline policy identities")
        identities.add(
            (
                planned.policy_sha256,
                planned.bindings_sha256,
                baseline_digest,
                adapter["commit"],
                adapter["tree"],
            )
        )
        results.append({"revision": revision, "diagnostics": diagnostic})
    if len(identities) != 1:
        raise ValueError(
            "diagnostics cannot combine different policy, registry or adapter identities"
        )
    policy, bindings, baseline_policy, adapter_commit, adapter_tree = identities.pop()
    summaries = [item["diagnostics"] for item in results]
    comparison_rows = [item["comparison"] for item in summaries]
    report = {
        "test-replay-diagnostics-version": 1,
        "repository": "dariuszpanas/django-ray",
        "activation": "disabled",
        "scope": "recorded selection reasons and baseline comparison; not dependency coverage",
        "source_replay_sha256": hashlib.sha256(replay_bytes).hexdigest(),
        "policy_sha256": policy,
        "bindings_sha256": bindings,
        "baseline_policy_sha256": baseline_policy,
        "adapter_source": {"commit": adapter_commit, "tree": adapter_tree},
        "summary": {
            "revisions": len(cohort),
            "selection_breadth": {
                key: sum(item["selection_breadth"] == key for item in summaries)
                for key in ("all", "partial", "empty")
            },
            "fallback_causes": {
                key: sum(item["fallback_cause"] == key for item in summaries)
                for key in ("profile-full", "full-trigger", "unknown-path", "selective")
            },
            "baseline_dispatches": sum(len(item["baseline"]) for item in comparison_rows),
            "planned_dispatches": sum(len(item["selected"]) for item in comparison_rows),
            "additional_dispatches": sum(len(item["additional"]) for item in comparison_rows),
            "omitted_dispatches": sum(len(item["missing"]) for item in comparison_rows),
            "additional_selection_revisions": sum(
                bool(item["additional"]) for item in comparison_rows
            ),
            "omission_revisions": sum(bool(item["missing"]) for item in comparison_rows),
            "test_runtime_observations": 0,
            "feedback_time_reduction": None,
        },
        "path_revisions": {
            key: _rank_paths(summaries, key)
            for key in (
                "full_trigger_paths",
                "unknown_paths",
                "no_impact_paths",
                "paths_without_recorded_classification",
            )
        },
        "suite_revisions": [
            {
                "suite": "qualification." + job,
                **{
                    key: sum("qualification." + job in item[key] for item in comparison_rows)
                    for key in ("baseline", "selected", "additional", "missing")
                },
            }
            for job in sorted(JOBS)
        ],
        "observations": results,
    }
    encode_replay(report)  # Complete output, including newline, must fit; never truncate.
    return report


def _job_selection(comparison: object, field: str) -> set[str]:
    if type(comparison) is not dict:
        raise ValueError("diagnostics require a comparison object")
    values = comparison.get(field)
    if (
        type(values) is not list
        or len(values) > len(JOBS)
        or any(type(value) is not str or value not in JOBS for value in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("diagnostics require unique known comparison jobs")
    return set(cast(list[str], values))


def _rank_paths(summaries: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for summary in summaries:
        for path in summary[field] or ():
            counts[path] = counts.get(path, 0) + 1
    return [
        {"path": path, "revisions": count}
        for path, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consumer", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--adapter-commit", required=True)
    parser.add_argument("--adapter-tree", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--diagnostics-output",
        type=Path,
        help="also retain a separate bounded selection-only diagnostics report",
    )
    args = parser.parse_args()
    if args.diagnostics_output is not None and (
        args.diagnostics_output.resolve() == args.output.resolve()
        or args.output.exists()
        or args.diagnostics_output.exists()
    ):
        parser.error("replay and diagnostic outputs must name distinct new files")
    for value in (args.adapter_commit, args.adapter_tree):
        if OID.fullmatch(value) is None:
            parser.error("adapter identities must be exact Git objects")
    with args.cohort.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        parser.error("cohort exceeds the input limit")
    try:
        consumer = verify_consumer_checkout(args.consumer, args.adapter_commit, args.adapter_tree)
    except YagaError:
        parser.error("consumer must be a clean checkout of the expected adapter commit and tree")
    adapter_path = consumer / "scripts/test_planning_shadow.py"
    spec = importlib.util.spec_from_file_location("shadow_replay_adapter", adapter_path)
    if spec is None or spec.loader is None:
        parser.error("cannot load the explicitly trusted consumer adapter")
    sys.path.insert(0, str(consumer))
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    try:
        verify_consumer_checkout(consumer, args.adapter_commit, args.adapter_tree)
    except YagaError:
        parser.error("consumer checkout changed while importing the adapter")
    document = replay(
        adapter,
        json.loads(raw),
        repository=consumer,
        policy_path=consumer / ".yaga/test-planning-shadow.toml",
        baseline_path=consumer / "scripts/qualification_policy.json",
        adapter_commit=args.adapter_commit,
        adapter_tree=args.adapter_tree,
    )
    document["adapter_file_sha256"] = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    document["replay_file_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    try:
        verify_consumer_checkout(consumer, args.adapter_commit, args.adapter_tree)
    except YagaError:
        parser.error("consumer checkout changed while replaying the cohort")
    try:
        encoded = encode_replay(document)
        diagnostic_bytes = (
            encode_replay(replay_diagnostics(document))
            if args.diagnostics_output is not None
            else None
        )
    except (ValueError, YagaError):
        parser.error("replay or diagnostics are invalid or exceed the output limit")
    with args.output.open("xb") as stream:
        stream.write(encoded)
    if diagnostic_bytes is not None:
        with args.diagnostics_output.open("xb") as stream:
            stream.write(diagnostic_bytes)
    print(json.dumps(document["summary"], sort_keys=True))
    return 1 if document["summary"]["omissions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
