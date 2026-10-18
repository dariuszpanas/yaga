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

MAX_BYTES = 4 * 1024 * 1024
OID = re.compile(r"[0-9a-f]{40}", re.ASCII)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consumer", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--adapter-commit", required=True)
    parser.add_argument("--adapter-tree", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
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
    except ValueError:
        parser.error("complete replay exceeds the output limit")
    with args.output.open("xb") as stream:
        stream.write(encoded)
    print(json.dumps(document["summary"], sort_keys=True))
    return 1 if document["summary"]["omissions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
