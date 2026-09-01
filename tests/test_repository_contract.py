"""Repository-level trust-boundary checks for the distributed action."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
FULL_SHA = re.compile(r"dariuszpanas/yaga@([0-9a-f]{40})(?:\s|$)")
RESERVED_STATUS_NAMES = {"codex review", "ci gate"}
COMMIT_CHECK_ACTION = (
    "dariuszpanas/yaga/actions/commit-check@407c9ba487e8b8aa15476f6884f9b8df43100c8e"
)
CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def section_keys(document: str, section: str, *, indent: int = 0) -> set[str]:
    """Return literal mapping keys immediately below a simple YAML section."""
    prefix = " " * indent
    lines = document.splitlines()
    marker = f"{prefix}{section}:"
    try:
        start = lines.index(marker) + 1
    except ValueError as error:
        raise AssertionError(section) from error
    child_prefix = " " * (indent + 2)
    keys: set[str] = set()
    for line in lines[start:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        match = re.fullmatch(
            rf"{re.escape(child_prefix)}([a-z][a-z0-9_-]*):.*",
            line,
        )
        if match is not None:
            keys.add(match.group(1))
    return keys


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
        rf"^  {re.escape(job)}:\n(?P<body>.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)",
        workflow,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def test_action_has_one_closed_direct_publisher_interface() -> None:
    action = read("action.yml")
    smoke = read("scripts/composite_smoke_python")

    assert section_keys(action, "inputs") == {
        "gate",
        "approval-marker",
        "github-token",
        "job-timeout-minutes",
        "lifecycle-workflow",
        "operation",
        "owner-id",
        "prerequisite-workflow",
        "request-timeout",
    }
    assert section_keys(action, "outputs") == {"pull_request_number", "route"}
    for legacy in (
        "mode:",
        "candidate:",
        "observer-workflow-path:",
        "poll-interval:",
        "poll-timeout:",
        "status-context:",
    ):
        assert legacy not in action

    assert "YAGA_GATE" in action
    assert "YAGA_APPROVAL_MARKER" in action
    assert "YAGA_OPERATION" in action
    assert "YAGA_JOB_TIMEOUT_MINUTES" in action
    assert "YAGA_REQUEST_TIMEOUT" in action
    assert "YAGA_MODE" not in action
    assert "YAGA_CANDIDATE" not in action
    assert "YAGA_OBSERVER" not in action
    assert 'python -P -S -m yaga gate "$YAGA_GATE" "$YAGA_OPERATION"' in action
    assert 'YAGA_ACTION_RUNTIME: "1"' in action
    assert "pip install" not in action
    assert "uv " not in action
    assert 'test "${YAGA_REQUEST_TIMEOUT:-}" = 5' in smoke
    assert 'test -z "${YAGA_APPROVAL_MARKER:-}"' in smoke

    owner = re.search(
        r"^  owner-id:\n(?P<body>.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)",
        action,
        re.MULTILINE | re.DOTALL,
    )
    assert owner is not None
    assert "required: true" in owner.group("body")
    assert "default:" not in owner.group("body")


def test_commit_action_is_a_separate_read_only_closed_interface() -> None:
    action = read("actions/commit-check/action.yml")
    workflow = read(".github/workflows/commit-policy.yml")

    assert "inputs:" not in action
    assert "outputs:" not in action
    assert re.search(r"actions/setup-python@[0-9a-f]{40}", action)
    assert 'token: ""' in action
    assert 'YAGA_COMMIT_ACTION_RUNTIME: "1"' in action
    assert "YAGA_ACTION_RUNTIME" not in action
    assert "github-token" not in action.casefold()
    assert "GITHUB_TOKEN" not in action
    assert "pip install" not in action
    assert "uv " not in action
    assert "python -P -S -m yaga github pull-request check" in action

    assert "types: [opened, synchronize, reopened, edited, ready_for_review]" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "pull-requests: write" not in workflow
    assert "statuses: write" not in workflow
    assert "secrets:" not in workflow
    assert "cancel-in-progress: true" in workflow
    assert "ref: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "fetch-depth: 0" in workflow
    assert "persist-credentials: false" in workflow
    assert "uses: ./actions/commit-check" in workflow


def test_commit_policy_example_is_copy_ready_and_immutably_pinned() -> None:
    example = read("examples/commit-policy.yml")
    readme = read("README.md")

    assert COMMIT_CHECK_ACTION in example
    assert COMMIT_CHECK_ACTION in readme
    assert "uses: ./actions/commit-check" not in example
    assert CHECKOUT_ACTION in example
    assert "pull_request:" in example
    assert "types: [opened, synchronize, reopened, edited, ready_for_review]" in example
    assert section_keys(example, "permissions") == {"contents"}
    assert section_keys(example, "jobs") == {"commit-policy"}
    assert "permissions:\n  contents: read" in example
    assert "pull-requests: write" not in example
    assert "statuses: write" not in example
    assert "secrets:" not in example
    assert "group: yaga-commit-policy-${{ github.event.pull_request.number }}" in example
    assert "cancel-in-progress: true" in example
    assert "ref: ${{ github.event.pull_request.head.sha }}" in example
    assert "fetch-depth: 0" in example
    assert "persist-credentials: false" in example
    assert "name: Commit Messages" in example
    assert "timeout-minutes: 5" in example
    for unsupported in ("pull_request_target:", "workflow_run:", "push:", "merge_group:"):
        assert unsupported not in example


def test_pre_commit_provider_is_one_closed_file_adapter() -> None:
    manifest = read(".pre-commit-hooks.yaml")
    readme = read("README.md")

    assert manifest == (
        "- id: yaga-commit-check\n"
        "  name: YAGA commit check\n"
        "  description: Validate the pending commit message with repository-local YAGA policy.\n"
        "  entry: yaga commit check --file\n"
        "  language: python\n"
        "  stages: [commit-msg]\n"
        "  pass_filenames: true\n"
        '  minimum_pre_commit_version: "3.2.0"\n'
    )
    assert "repo: https://github.com/dariuszpanas/yaga" in readme
    assert "rev: 407c9ba487e8b8aa15476f6884f9b8df43100c8e" in readme
    assert "id: yaga-commit-check" in readme
    assert "pre-commit install --hook-type commit-msg --install-hooks" in readme


def test_footer_policy_contract_is_documented_for_maintainers_and_users() -> None:
    readme = read("README.md")
    contributing = read("CONTRIBUTING.md")
    agents = read("AGENTS.md")

    for document in (readme, contributing, agents):
        assert "`required-footer-tokens`" in document
        assert "`forbidden-footer-tokens`" in document
        assert "`Signed-off-by`" in document
        assert "DCO" in document
        assert "`footer.required`" in document
        assert "`footer.forbidden`" in document

    assert "at most 128 entries combined" in readme
    assert "pull-request title's header-only check" in readme


def test_per_type_scope_policy_contract_is_documented_and_dogfooded() -> None:
    readme = read("README.md")
    contributing = read("CONTRIBUTING.md")
    agents = read("AGENTS.md")

    for document in (readme, contributing, agents):
        assert "`scope-policy-by-type`" in document
        assert "`scope.required`" in document
        assert "`scope.forbidden`" in document
        assert "conservative structural" in document

    assert "at most 128 safe type tokens" in readme
    assert "complete commits and pull-request\ntitles" in readme


def test_prerequisite_ci_names_and_triggers_the_exact_source_boundary() -> None:
    ci = read(".github/workflows/ci.yml")
    readme = read("README.md")
    contributing = read("CONTRIBUTING.md")

    assert "pull_request:\n    types: [opened, synchronize, reopened, ready_for_review]" in ci
    assert "github.event_name == 'pull_request'" in ci
    assert "github.event.action" in ci
    assert "github.event.pull_request.number" in ci
    assert "github.event.pull_request.base.sha" in ci
    assert "YAGA CI {0} for #{1} at base {2}" in ci
    assert "name: CI Prerequisites" in workflow_job(ci, "gate")
    assert "name: CI Gate" not in ci
    for document in (readme, contributing):
        assert "YAGA CI <action> for #<pull-request> at base <full-base-SHA>" in document
        assert "ready_for_review" in document


def test_recommended_v3_is_explicitly_dogfooded_and_bounded() -> None:
    makefile = read("Makefile")
    ci = read(".github/workflows/ci.yml")
    readme = read("README.md")
    contributing = read("CONTRIBUTING.md")
    agents = read("AGENTS.md")

    for gate in (makefile, ci):
        assert "--workflow-security-profile recommended-v3" in gate
        assert "--workflow-security-profile recommended-v2" not in gate

    for document in (readme, contributing, agents):
        assert "`recommended-v1`" in document
        assert "`recommended-v2`" in document
        assert "`recommended-v3`" in document
        assert "`permissions.pull_request_write`" in document
        assert "`pull-requests: write`" in document
        assert "`statuses: write`" in document
        assert "`pull_request_target`" in document
        assert "`workflow_run`" in document

    assert "With no selection options, YAGA still selects the frozen `recommended-v1`" in readme
    assert "A mixed-event workflow remains pull-request-triggered" in readme
    assert "a job-level `if` condition" in readme
    assert "does not cover other write scopes, alternate" in readme
    assert "tokens, reusable-workflow permission inheritance, or expressions" in readme


def test_examples_split_lifecycle_invalidation_from_post_ci_publication() -> None:
    lifecycle = read("examples/review-policy.yml")
    publisher = read("examples/codex-review.yml")

    assert {path.name for path in (ROOT / "examples").glob("*.yml")} == {
        "codex-review.yml",
        "commit-policy.yml",
        "review-policy.yml",
    }
    assert lifecycle.startswith("name: YAGA Review Policy\n")
    assert "github.event.action == 'edited' && github.event.changes.base == null" in lifecycle
    assert "YAGA metadata edit for #{0}" in lifecycle
    assert "YAGA {0} boundary for #{1}" in lifecycle
    assert section_keys(lifecycle, "jobs") == {"invalidate"}
    invalidator = workflow_job(lifecycle, "invalidate")
    assert "'Review Policy Metadata' || 'Review Policy Boundary'" in invalidator
    assert "github.event.action == 'edited' && github.event.changes.base == null" in invalidator
    assert "pull_request_target:" in lifecycle
    assert "branches: [main]" in lifecycle
    assert (
        "types: [opened, synchronize, reopened, edited, ready_for_review, converted_to_draft]"
    ) in lifecycle
    for unsupported in (
        "workflow_run:",
        "issue_comment:",
        "pull_request_review:",
        "schedule:",
        "workflow_dispatch:",
        "merge_group:",
        "closed",
    ):
        assert unsupported not in lifecycle

    assert publisher.startswith("name: YAGA Codex Review Publisher\n")
    assert section_keys(publisher, "jobs") == {
        "prepare",
        "observe",
        "request-owner",
        "authorize-external",
        "request-external",
        "finalize",
    }
    assert "workflow_run:" in publisher
    assert 'workflows: [CI, "YAGA Review Policy"]' in publisher
    assert "format('YAGA review wake from {0} run #{1}'" in publisher
    assert "types: [completed]" in publisher
    assert "branches-ignore:" not in publisher
    for unsupported in (
        "pull_request_target:",
        "issue_comment:",
        "pull_request_review:",
        "schedule:",
        "workflow_dispatch:",
        "merge_group:",
    ):
        assert unsupported not in publisher

    combined = lifecycle + publisher
    assert "toJSON(" not in combined
    assert "fromJSON(" not in combined
    assert '"action"' not in combined
    assert not (ROOT / "examples" / "review-policy-event.yml").exists()


def test_examples_have_closed_routing_and_literal_external_approval() -> None:
    publisher = read("examples/codex-review.yml")
    prepare = workflow_job(publisher, "prepare")
    observe = workflow_job(publisher, "observe")
    owner = workflow_job(publisher, "request-owner")
    authorization = workflow_job(publisher, "authorize-external")
    external = workflow_job(publisher, "request-external")
    finalize = workflow_job(publisher, "finalize")

    assert "github.event.workflow_run.path == '.github/workflows/ci.yml'" in prepare
    assert "github.event.workflow_run.event == 'pull_request'" in prepare
    assert "github.event.workflow_run.path == '.github/workflows/review-policy.yml'" in prepare
    assert "github.event.workflow_run.event == 'pull_request_target'" in prepare
    assert "route: ${{ steps.prepare.outputs.route }}" in prepare
    assert "pull_request_number: ${{ steps.prepare.outputs.pull_request_number }}" in prepare
    assert "if: needs.prepare.outputs.route == 'observe'" in observe
    assert "if: needs.prepare.outputs.route == 'owner'" in owner
    assert "if: needs.prepare.outputs.route == 'external'" in authorization
    assert "needs.prepare.outputs.route == 'approved'" in external
    assert "needs.authorize-external.result == 'success'" in external
    assert "environment:" not in owner
    assert (
        "environment:\n      name: codex-review-approval\n      deployment: false" in authorization
    )
    assert "environment:" not in external
    assert "operation: authorize" in authorization
    assert "approval-marker: ${{ vars.YAGA_CODEX_APPROVAL_MARKER }}" in authorization
    assert (
        "needs: [prepare, observe, request-owner, authorize-external, request-external]" in finalize
    )
    assert "always()" in finalize
    assert "needs.prepare.result == 'success'" in finalize
    assert "needs.prepare.outputs.route != 'skip'" in finalize


def test_example_jobs_have_exact_least_privilege_permissions() -> None:
    lifecycle = read("examples/review-policy.yml")
    publisher = read("examples/codex-review.yml")
    invalidate = workflow_job(lifecycle, "invalidate")
    prepare = workflow_job(publisher, "prepare")
    observe = workflow_job(publisher, "observe")
    owner = workflow_job(publisher, "request-owner")
    authorization = workflow_job(publisher, "authorize-external")
    external = workflow_job(publisher, "request-external")
    finalize = workflow_job(publisher, "finalize")

    assert "permissions: {}" in lifecycle
    assert "permissions: {}" in publisher
    assert section_keys(invalidate, "permissions", indent=4) == {
        "actions",
        "contents",
        "pull-requests",
        "statuses",
    }
    for job in (prepare, observe, finalize):
        assert section_keys(job, "permissions", indent=4) == {
            "actions",
            "contents",
            "issues",
            "pull-requests",
            "statuses",
        }
        assert "issues: read" in job

    for job in (owner, external):
        assert section_keys(job, "permissions", indent=4) == {
            "actions",
            "contents",
            "pull-requests",
            "statuses",
        }
        assert "pull-requests: write" in job
    assert section_keys(authorization, "permissions", indent=4) == {
        "actions",
        "contents",
        "pull-requests",
        "statuses",
    }
    assert "pull-requests: write" in authorization
    assert "statuses: read" in authorization

    for workflow in (lifecycle, publisher):
        assert "checks:" not in workflow
        assert "contents: write" not in workflow
        assert "issues: write" not in workflow
    assert "pull-requests: write" not in lifecycle
    for job in (prepare, observe, finalize):
        assert "pull-requests: write" not in job


def test_example_jobs_execute_only_pinned_closed_operations() -> None:
    lifecycle = read("examples/review-policy.yml")
    publisher = read("examples/codex-review.yml")
    jobs = {
        "invalidate": (workflow_job(lifecycle, "invalidate"), "invalidate"),
        "prepare": (workflow_job(publisher, "prepare"), "prepare"),
        "observe": (workflow_job(publisher, "observe"), "observe"),
        "request-owner": (workflow_job(publisher, "request-owner"), "request"),
        "authorize-external": (workflow_job(publisher, "authorize-external"), "authorize"),
        "request-external": (workflow_job(publisher, "request-external"), "request"),
        "finalize": (workflow_job(publisher, "finalize"), "finalize"),
    }

    for job, operation in jobs.values():
        assert FULL_SHA.findall(job) == ["0" * 40]
        assert len(re.findall(r"^      - name:", job, re.MULTILINE)) == 1
        assert job.count("\n        uses: dariuszpanas/yaga@") == 1
        assert f"operation: {operation}" in job
        assert "prerequisite-workflow: .github/workflows/ci.yml" in job
        assert "lifecycle-workflow: .github/workflows/review-policy.yml" in job
        assert "owner-id: ${{ vars.YAGA_CODEX_OWNER_ID }}" in job
        assert "\n        run:" not in job
        assert "actions/checkout" not in job
        assert "upload-artifact" not in job
        assert "download-artifact" not in job
        assert "cache" not in job.casefold()
        assert len(re.findall(r"^    timeout-minutes: 15$", job, re.MULTILINE)) == 1
        assert len(re.findall(r"^          job-timeout-minutes: 15$", job, re.MULTILINE)) == 1
        assert "mode:" not in job
        assert "candidate:" not in job


def test_examples_serialize_exact_boundaries_without_post_close_or_main_push_wakes() -> None:
    lifecycle = read("examples/review-policy.yml")
    publisher = read("examples/codex-review.yml")

    assert "yaga-review-policy-${{ github.event.pull_request.number }}-${{" in lifecycle
    assert "github.run_id || 'boundary'" in lifecycle
    assert "yaga-codex-approval-${{ github.repository_id }}-${{" in publisher
    assert "yaga-codex-worker-${{ github.repository_id }}-${{" in publisher
    assert "cancel-in-progress: true" in lifecycle
    authorization = workflow_job(publisher, "authorize-external")
    assert "cancel-in-progress: true" in authorization
    for job_name in ("observe", "request-owner", "request-external"):
        assert "cancel-in-progress: false" in workflow_job(publisher, job_name)
    assert "closed" not in lifecycle
    prepare = workflow_job(publisher, "prepare")
    assert "github.event.workflow_run.path == '.github/workflows/ci.yml'" in prepare
    assert "github.event.workflow_run.event == 'pull_request'" in prepare
    assert "github.event.workflow_run.path == '.github/workflows/review-policy.yml'" in prepare
    assert "github.event.workflow_run.event == 'pull_request_target'" in prepare


def test_docs_define_bounded_polling_and_explicit_rerun_recovery() -> None:
    readme = read("README.md")
    contributing = read("CONTRIBUTING.md")

    assert "Polling is bounded" in readme
    assert "rerun CI" in readme
    assert "There is no scheduled repair" in readme
    assert re.search(r"no\s+schedule, issue-comment", contributing)
    assert "job-timeout-minutes" in contributing
    assert "sole step" in contributing


def test_docs_require_strict_status_threads_and_reject_merge_queues() -> None:
    readme = read("README.md")

    assert "Require strict, up-to-date" in readme
    assert "required conversation resolution" in readme
    assert "Review Policy Boundary" in readme
    assert "CI Prerequisites" in readme
    assert "Merge queues are unsupported" in readme
    assert "no `merge_group` trigger" in readme


def test_docs_explain_direct_writer_authority_and_audit_limit() -> None:
    readme = read("README.md")
    security = read("SECURITY.md")

    assert "classic commit status" in readme
    assert re.search(r"both must\s+pass", readme)
    assert "shared GitHub Actions integration" in readme
    assert "statuses: write" in readme
    assert re.search(r"reserve\s+every\s+case-insensitive `Codex Review` alias", readme)
    assert "dedicated YAGA GitHub App" in readme
    assert "same-repository workflow remains" in security
    assert "Commit-status publication is not transactional" in security


def test_docs_define_the_single_quota_guarded_review_request() -> None:
    readme = read("README.md")
    security = read("SECURITY.md")

    assert "posts at most one strictly marked quota-consuming request" in readme
    assert "codex-review-approval" in readme
    assert (
        "Only that protected route's exact YAGA marker authorizes an external-author request"
        in (readme)
    )
    assert re.search(
        r"Only the protected route's exact approval marker\s+authorizes YAGA to request review",
        security,
    )
    assert "Disable Codex automatic reviews" in readme
    assert "drain every existing Codex task" in readme
    assert re.search(r"An eyes reaction is progress, not\s+success", readme)
    assert re.search(
        r"Every outcome must be strictly later than the exact current-boundary Actions-owned YAGA "
        r"request\s+marker, including on the initial non-draft `opened` boundary",
        readme,
    )
    assert re.search(
        r"`observe` route is available only for\s+that already-posted exact request", security
    )
    assert re.search(
        r"Visible unsolicited connector activity fails closed without\s+(?:posting )?a duplicate "
        r"YAGA request",
        readme,
    )
    assert "External approval never reuses unsolicited evidence" in readme
    assert "delayed review of an older head" in readme
    assert "no direct or other\nintegration-triggered Codex review can overlap YAGA" in readme
    assert "trusted successful status\nlineage for every older YAGA request" in readme
    assert "More than eight older YAGA\nrequest boundaries also fail closed" in readme
    assert "must not enable this beta action" in readme
    assert "directly posting `@codex review`" in readme
    assert "temporal correlation, not a native provider binding" in readme
    assert "initial reaction-only success is accepted" not in security.casefold()


def test_docs_explain_the_delayed_invalidator_close_boundary() -> None:
    readme = read("README.md")
    contributing = read("CONTRIBUTING.md")
    security = read("SECURITY.md")

    assert "skips a PR that is\nalready closed" in readme
    assert re.search(r"race an already-running\s+worker's final live read", readme)
    assert "does not\nguarantee zero post-close writes" in readme
    assert re.search(r"Treat this as a\s+bounded residual", contributing)
    assert "cannot guarantee zero post-close writes" in security


def test_workflow_job_and_step_names_do_not_impersonate_the_commit_status() -> None:
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
        assert not RESERVED_STATUS_NAMES.intersection(names), workflow


def test_ci_actions_are_pinned_to_full_commit_shas() -> None:
    ci = read(".github/workflows/ci.yml")
    uses = re.findall(r"^\s+- uses:\s+([^\s#]+)", ci, re.MULTILINE)

    assert uses
    for reference in uses:
        assert reference == "./" or re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference)

    assert "Exercise the composite action manifest and wiring" in ci
    assert "mode: resolve" not in ci
    assert "steps.action_smoke.outputs" not in ci
    assert "continue-on-error" not in ci
