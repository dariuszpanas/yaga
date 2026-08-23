"""Repository-level trust-boundary checks for the distributed action."""

from __future__ import annotations

import json
import re
from pathlib import Path

from yaga.codex.constants import (
    DEFAULT_OBSERVER_WORKFLOW_PATH,
    MAX_OPEN_PULL_REQUESTS,
    MAX_SCHEDULE_CANDIDATES,
    MAX_SCHEDULE_RECONCILE_REQUESTS,
    SCHEDULE_INTERVAL_MINUTES,
    SOURCE_TITLE_KEYS,
)

ROOT = Path(__file__).parents[1]
FULL_SHA = re.compile(r"dariuszpanas/yaga@([0-9a-f]{40})(?:\s|$)")


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def normalized_literal_name(line: str) -> str | None:
    """Normalize one literal YAML name enough to detect status collisions."""
    match = re.fullmatch(r"\s*name:\s*(.*?)\s*(?:#.*)?", line)
    if match is None:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
        value = value[1:-1]
    return " ".join(value.split()).casefold()


def workflow_job(workflow: str, job: str) -> str:
    match = re.search(
        rf"^  {re.escape(job)}:\n(?P<body>.*?)(?=^  [a-z][a-z-]*:\n|\Z)",
        workflow,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def test_action_keeps_the_status_identity_inside_the_gate_adapter() -> None:
    action = read("action.yml")

    assert "status-context:" not in action
    assert "CODEX_REVIEW_STATUS_CONTEXT" not in action
    assert "YAGA_CANDIDATE" in action
    assert "YAGA_OBSERVER_WORKFLOW_PATH" in action
    assert "publisher-workflow-path" not in action
    assert "YAGA_PUBLISHER_WORKFLOW_PATH" not in action


def test_observer_is_read_only_and_records_close_without_codex_publication() -> None:
    observer = read("examples/review-policy-event.yml")

    assert "permissions: {}" in observer
    assert "contents: read" not in observer
    assert "statuses: write" not in observer
    assert "actions/checkout" not in observer
    assert "upload-artifact" not in observer
    assert "closed" in observer


def test_observer_schema_and_publisher_references_are_one_protocol() -> None:
    observer = read("examples/review-policy-event.yml")
    publisher = read("examples/codex-review.yml")
    action = read("action.yml")

    assert observer.startswith("name: Review Policy Event\n")
    assert set(re.findall(r'"([a-z_]+)":', observer)) == SOURCE_TITLE_KEYS
    assert (
        "  pull_request_target:\n"
        "    types: [opened, synchronize, reopened, edited, ready_for_review, "
        "converted_to_draft, closed]\n"
    ) in observer
    assert "  pull_request_review:\n    types: [submitted, edited, dismissed]\n" in observer
    assert "workflows: [Review Policy Event]" in publisher
    assert (
        publisher.count(f"github.event.workflow_run.path == '{DEFAULT_OBSERVER_WORKFLOW_PATH}'")
        == 2
    )
    assert publisher.count(f"observer-workflow-path: {DEFAULT_OBSERVER_WORKFLOW_PATH}") == 6
    assert f"default: {DEFAULT_OBSERVER_WORKFLOW_PATH}" in action
    assert observer.index('  "action":"${{ github.event.action }}",') < observer.index(
        '  "base_ref":${{ toJSON(github.event.pull_request.base.ref) }},'
    )


def test_closed_guard_survives_truncation_and_escaped_base_ref_spoofing() -> None:
    token = '"action":"closed"'
    closed_title = json.dumps(
        {"v": 1, "action": "closed", "base_ref": "x" * 255},
        separators=(",", ":"),
    )
    spoofed_title = json.dumps(
        {"v": 1, "action": "opened", "base_ref": token + "x" * 255},
        separators=(",", ":"),
    )

    assert token in closed_title[:64]
    assert token not in spoofed_title
    assert token not in spoofed_title[:64]


def test_readme_requires_strict_currency_and_documents_reaction_limitations() -> None:
    readme = read("README.md")

    assert "requires `Codex Review` to be an up-to-date/strict required status" in readme
    assert "it is not a substitute for strict branch" in readme
    assert "draft-to-ready review that produces only a `+1` reaction stays" in readme
    assert "also treats it as a defensive runtime no-op" in readme
    assert "A `converted_to_draft` transition instead persists pending" in readme
    assert "required conversation resolution is a v1 deployment prerequisite" in readme


def test_publisher_executes_only_pinned_yaga_code() -> None:
    publisher = read("examples/codex-review.yml")
    references = FULL_SHA.findall(publisher)

    assert references
    assert set(references) == {"0" * 40}
    assert "pull_request_target:" not in publisher
    assert "actions/checkout" not in publisher
    assert "checks: write" not in publisher
    assert "statuses: write" in publisher
    assert "queue:" not in publisher
    assert '!contains(github.event.workflow_run.display_title, \'"action":"closed"\')' in publisher
    assert (
        publisher.count(
            "    concurrency:\n"
            "      group: yaga-codex-review-${{ github.event.workflow_run.head_sha }}\n"
        )
        == 1
    )
    assert (
        publisher.count(
            "    concurrency:\n      group: yaga-codex-review-${{ matrix.candidate.head }}\n"
        )
        == 2
    )
    assert (
        "    concurrency:\n      group: yaga-codex-review-repair-${{ github.repository }}\n"
    ) in publisher


def test_terminal_jobs_pin_the_declared_window_and_run_only_yaga() -> None:
    publisher = read("examples/codex-review.yml")
    action = read("action.yml")

    assert "job-timeout-minutes:" in action
    assert "YAGA_JOB_TIMEOUT_MINUTES" in action
    for job in ("lifecycle", "reconcile", "reconcile-repair"):
        block = workflow_job(publisher, job)
        assert len(re.findall(r"^    timeout-minutes: 15$", block, re.MULTILINE)) == 1
        assert len(re.findall(r"^          job-timeout-minutes: 15$", block, re.MULTILINE)) == 1
        assert block.count("\n      - uses: dariuszpanas/yaga@") == 1
        assert "\n      - name:" not in block


def test_scheduled_repair_stays_below_the_public_repository_rate_limit() -> None:
    # One repair pass reads repository + open-PR state, scans history/status,
    # revalidates the default once, then re-reads and writes every unsafe PR.
    repair_requests = 3 + 4 * MAX_OPEN_PULL_REQUESTS
    terminal_requests = MAX_SCHEDULE_CANDIDATES * MAX_SCHEDULE_RECONCILE_REQUESTS
    passes_per_hour = 60 // SCHEDULE_INTERVAL_MINUTES
    scheduled_requests_per_hour = passes_per_hour * (repair_requests + terminal_requests)

    assert repair_requests == 163
    assert scheduled_requests_per_hour == 710
    assert scheduled_requests_per_hour <= 800


def test_v1_requires_strict_status_and_rejects_merge_queue_claims() -> None:
    readme = read("README.md")

    assert "requires `Codex Review` to be an up-to-date/strict required status" in readme
    assert "Merge queues are unsupported in v1" in readme
    assert "merge queue" not in readme.replace("Merge queues are unsupported in v1", "")


def test_action_does_not_import_from_the_caller_working_tree() -> None:
    action = read("action.yml")

    assert 'export PYTHONPATH="$GITHUB_ACTION_PATH/src"' in action
    assert "PYTHONPATH:+" not in action
    assert "export PYTHONSAFEPATH=1" in action
    assert "python -P -m yaga" in action


def test_workflow_job_names_do_not_impersonate_the_commit_status() -> None:
    assert normalized_literal_name('    name: " cOdEx   ReVieW "') == "codex review"
    workflows = [
        *(ROOT / "examples").glob("*.yml"),
        *(ROOT / ".github" / "workflows").glob("*.yml"),
        *(ROOT / ".github" / "workflows").glob("*.yaml"),
    ]
    for workflow in workflows:
        names = {
            normalized
            for line in workflow.read_text(encoding="utf-8").splitlines()
            if (normalized := normalized_literal_name(line)) is not None
        }
        assert "codex review" not in names, workflow


def test_readme_reserves_case_insensitive_status_and_check_aliases() -> None:
    readme = read("README.md")

    assert "status contexts case-insensitively" in readme
    assert re.search(r"reserve\s+every\s+case-insensitive alias", readme)
    assert "reject colliding workflow, job, or check names" in readme


def test_ci_actions_are_pinned_to_full_commit_shas() -> None:
    ci = read(".github/workflows/ci.yml")
    uses = re.findall(r"^\s+- uses:\s+([^\s#]+)", ci, re.MULTILINE)

    assert uses
    for reference in uses:
        assert reference == "./" or re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference)

    assert "Exercise the composite action manifest and wiring" in ci
    assert "Require the composite smoke outputs" in ci
    assert "continue-on-error" not in ci
