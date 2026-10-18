# Executor handoff and shadow adoption

YAGA can validate a test plan against a consumer-owned registry of fixed jobs and emit a bounded
handoff. Existing executors still own setup, commands, runner permissions, timeouts, artifacts,
cancellation, and cleanup. The handoff contains job IDs and required cells; it contains no commands
and reports no test outcomes.

Phase 3 supplies this Python API, runnable examples, and a django-ray shadow-pilot adapter.
Historical selection replay and one full same-source qualification run are recorded; actual
selective feedback-time measurements and the adoption decision remain pending. Existing required
CI checks remain enabled and authoritative.
Phase 4B adds offline normalized receipt evaluation. Selective-execution activation remains later work.
Phase 4A adds pure selection diagnostics and reviewed application-test ownership mappings.

## Build a fixed registry

The API is installed with YAGA and stays outside both composite Action runtimes:

```python
from yaga.test_planning.handoff import ExecutorRegistry, SuiteExecutor, create_handoff
from yaga.test_planning.service import create_test_plan

# These values come from the consumer's reviewed execution manifest and source.
registry = ExecutorRegistry(
    (
        SuiteExecutor(
            suite="backend",
            binding="backend-unit",
            dispatch="backend",
            variants=("py312", "py315"),
            capabilities=("linux", "python"),
            definition_sha256=backend_definition_sha256,
        ),
    )
)
planned = create_test_plan(
    repository,
    policy_path=policy_file,
    profile="development",
    revision_range=explicit_range,
    bindings_sha256=registry.sha256,
)
handoff = create_handoff(
    planned,
    registry,
    expected_policy_sha256=trusted_policy_sha256,
    expected_execution_commit=expected_commit,
    expected_execution_tree=expected_tree,
)
```

This skeleton assumes a single-suite catalog. A real registry must map the **entire** catalog
exactly once, including currently unselected suites. Bindings and variant sets must match exactly;
each executor must declare all capabilities required by its suite. Dispatch IDs must be unique.
The adapter supplies expected policy/source identities independently and verifies the actual
execution checkout and environment. Copying those values from an untrusted plan is not verification.

Each `definition_sha256` identifies the immutable execution definition for that suite. Include
the workflow or fixed executor code, its source revision, locked dependency/tool/image identities,
configuration and matrix definition, and setup/cleanup contract. The consumer constructs and
retains these bytes; YAGA does not fetch or authenticate them. A declared `docker` capability does
not prove a Docker daemon exists: consumer preflight must check availability before dispatch.

`ExecutorRegistry.document()` emits version 1 with complete, sorted executor definitions.
`registry.sha256` hashes its canonical UTF-8 JSON (sorted keys, compact separators, no ASCII
escaping). Changing a definition, binding, variant, dispatch ID, or capability changes the manifest
identity. Declaration order does not. Supply this identity when creating the plan.

Handoff rejects preview plans with null binding identity, mismatched policy/source/registry
identities, missing or extra mappings, changed bindings/variants, and missing capabilities.
Invalid inputs fail before any dispatch document is returned. There is no execution command or
shell interpolation. The separate evaluator imports retained plans and normalized results offline.

## Normalize and evaluate receipts

The pure APIs live in `yaga.test_planning.results` and `yaga.test_planning.evaluation`:

```python
from yaga.test_planning.evaluation import encode_evaluation, evaluate_results
from yaga.test_planning.results import parse_results_document

# The adapter has already performed bounded JSON acquisition, duplicate-key rejection,
# provenance checks, and each executor's custom acceptance and cleanup validation.
results = parse_results_document(normalized_document)
evaluation = evaluate_results(
    planned,
    results,
    expected_policy_sha256=trusted_policy_sha256,
    expected_bindings_sha256=registry.sha256,
    expected_execution_commit=expected_commit,
    expected_execution_tree=expected_tree,
    expected_run_id=expected_run_id,
    expected_attempt=expected_attempt,
)
report_bytes = encode_evaluation(evaluation)
```

`CellResult`, `ResultOutcome`, and `TestResults` also support validated direct construction.
`results_document` and `encode_results` produce canonical output sorted by suite and variant.
The evaluation's `results_sha256` hashes that canonical result document without its trailing
newline. Reordering received cells does not change the fingerprint. Input parsers and output
encoders reject unsupported versions, malformed fields, and over-budget documents; see the
[complete CLI and wire contract](test-planning.md#evaluate-normalized-results).

The generic evaluator checks only the normalized contract. Consumers must verify these conditions
before emitting a passed cell:

| Executor | Adapter-owned acceptance |
| --- | --- |
| Backend/frontend tests | Expected collection and environment, successful assertions, no prohibited skips, and server/fixture cleanup |
| Docker checks | Correct immutable image/environment, scenario assertions, and owned-container cleanup |
| Chainsaw | Complete expected scenarios and assertions plus the declared cluster/resource cleanup or restoration contract |
| django-ray qualifications | Source-owned application, native, latency, cancellation, and monitoring receipt assertions for every selected variant, cold phases where applicable, and owned cleanup |

Keep raw evidence and its digest, exact source/manifest, authenticated run and final authorized
job attempt, and retry lineage. A provider's green job or artifact name alone cannot produce a
passed result. Missing evidence files are operational errors. Failed assertions or cleanup must
produce a failed cell or an operational error; a selected job that was skipped is blocked. Do not pick a passing
attempt from a different run or use older source receipts after an acceptance contract changes.

The django-ray phase 4B consumer is developed separately from the active 0.7 release. Its offline
normalizer does not replace the existing shadow aggregate, alter workflow conditions, or dispatch
jobs. Before adoption, reconcile it with the eventual release source, including new application
lease-policy and runtime-environment-maintenance facts and upgraded schema assertions. Its
separate experiment observations retain unavailable timings explicitly. The retained full pilot
informs regression fixtures, but is not evidence that a new source or adapter was executed.

## Version-one handoff

`create_handoff` accepts a validated `PlannedTests` value and returns a complete JSON-compatible
dictionary. It is bounded to 4 MiB without truncation.

| Field | Contract |
| --- | --- |
| `test-handoff-version` | Integer `1` |
| `plan_digest`, `policy_sha256`, `bindings_sha256` | Exact plan, policy-byte, and registry identities |
| `source` | Exact base/head/comparison/execution commit and execution-tree identities |
| `profile`, `requested_mode`, `effective_mode` | The plan's explicit profile and mode |
| `dispatch` | Complete sorted map of fixed dispatch IDs to booleans, including false entries |
| `selected` | Prerequisites first; one record per selected suite with binding, dispatch, variants, and prerequisite dispatch IDs in `needs` |
| `cells` | Every selected suite/variant obligation, with its binding and dispatch ID |

A false dispatch value means the suite is not selected by this plan. It never means its tests
passed. A missing output is an error, not an all-false map. Dispatch one fixed job per selected
record; do not dispatch a complete matrix separately for every cell. Preserve the existing DAG and
wait for prerequisites before dependent jobs.

Before enabling selective execution, an always-running aggregate must verify that every selected
cell completed successfully at the expected source, including setup, custom acceptance evidence,
and cleanup. Failed, cancelled, blocked, missing, or unexpectedly skipped work must not pass.
If cancellation prevents aggregation, required checks must remain unsatisfied. Phase 3 does not
replace any existing required aggregate.

## Run the two example adapters

The repository's
[`examples/test-planning/fixed_handoff.py`](https://github.com/dariuszpanas/yaga/blob/main/examples/test-planning/fixed_handoff.py)
contains literal maps for both catalogs. Run it from the YAGA checkout with locally available,
explicit object IDs:

```bash
uv run python examples/test-planning/fixed_handoff.py \
  --catalog polyglot --policy docs/designs/examples/test-planning/polyglot.toml \
  --profile development --repo /path/to/application --range BASE...CANDIDATE \
  --execution-commit EXACT_CANDIDATE_COMMIT --execution-tree EXACT_CANDIDATE_TREE

uv run python examples/test-planning/fixed_handoff.py \
  --catalog django-ray --policy docs/designs/examples/test-planning/django-ray.toml \
  --profile integration --repo /path/to/django-ray --revision CANDIDATE \
  --execution-commit EXACT_CANDIDATE_COMMIT --execution-tree EXACT_CANDIDATE_TREE
```

The adapter prints a handoff and executes nothing. Its definition digest covers its own exact
example source bytes; it does not identify a real consumer's tools, images, or workflows. Replace
that demonstration identity with the complete reviewed execution manifest before deployment.
These runnable examples demonstrate selection and handoff, not successful qualification or savings.

The [polyglot policy](designs/examples/test-planning/polyglot.toml) maps all five execution
categories and their supporting suites:

| Category | Suite → fixed dispatch | Executor preserves |
| --- | --- | --- |
| Backend | `backend.unit` → `backend-unit`; `backend.integration` → `backend-integration` | Python 3.12/3.15 cells; PostgreSQL setup and cleanup for integration |
| Frontend | `frontend.unit` → `frontend-unit`; `frontend.browser` → `frontend-browser` | Node dependency lock; browser environment and complete results |
| Docker | `image.build` → `image`; `container.smoke` → `container` | Exact source/image identity; smoke waits for image; owned resource cleanup |
| Chainsaw | `infra.chainsaw` → `chainsaw` | Image prerequisite; cluster/tool setup; assertion evidence and cleanup |
| Custom qualification | `qualification.ray` → `ray` | Image prerequisite; pinned Ray environment and custom acceptance evidence |
| Supporting checks | `repository.smoke` → `repository`; `docs.check` → `docs` | Mandatory smoke obligation and independent documentation checks |

The [django-ray policy](designs/examples/test-planning/django-ray.toml) has five fixed reusable
workflow bindings:

| Suite | Dispatch | Existing workflow | Required cells |
| --- | --- | --- | --- |
| `qualification.application` | `application` | `application-qualification.yml` | `default` |
| `qualification.native` | `native` | `upgrade-native-qualification.yml` | `core-loss`, `jobs`, `jobs-crash`, `native` |
| `qualification.latency` | `latency` | `latency-qualification.yml` | `default` |
| `qualification.cancellation` | `cancellation` | `cancellation-qualification.yml` | `default` |
| `qualification.monitoring` | `monitoring` | `monitoring-qualification.yml` | `default` |

Call the native workflow once; its existing serial four-cell matrix supplies the four obligations.
Do not launch four copies of that complete matrix. Keep application acceptance receipts, cancelled
driver observations, image/source checks, and every owned-resource cleanup check in their current
workflows. This catalog does not cover ordinary django-ray CI, which remains required separately.

## Explain selection without changing the plan

The installed Python API emits a separate `test-selection-diagnostics-version: 1` document;
plan, manifest, handoff, and consumer comparison version 1 remain unchanged:

```python
from yaga.test_planning.diagnostics import diagnose_selection, encode_diagnostics

# planned is the PlannedTests returned by create_test_plan above.
# Supply the current selector's catalog suite IDs, not executor dispatch IDs.
diagnostics = diagnose_selection(planned, baseline_suites=("qualification.application",))
payload = encode_diagnostics(diagnostics)
```

The report retains the original plan digest, exact policy/binding hashes, source identities, profile,
and requested/effective modes. It adds these independently interpretable fields:

| Field | Meaning |
| --- | --- |
| `selection_breadth` | `empty`, `partial`, or `all`, relative to the catalog; rules may select every suite while the effective mode stays selective |
| `fallback_cause` | `profile-full`, `full-trigger`, `unknown-path`, or `selective`; the last means no full-mode decision was recorded |
| `counts` | Catalog and selected suite/cell counts, plus changed-path count; a cell is one suite variant, not a workflow invocation |
| `full_trigger_paths`, `unknown_paths`, `no_impact_paths` | Unique sorted paths attributed by the recorded reasons |
| `paths_without_recorded_classification` | Changed paths lacking a recorded path reason; a full profile or early full trigger can bypass classification |
| `rules` | Matched rule IDs with unique sorted paths/suites and their counts |
| `comparison` | Sorted baseline, selected, additional, and missing catalog suite IDs |

Omit `baseline_suites` to report an unavailable comparison (`null`). An explicit empty tuple means
the baseline selected nothing. Duplicate or unknown baseline IDs are errors. For revision-only
plans, changed-path counts and path lists are `null`, since no range was collected.

These are summaries of recorded reasons, not a dependency coverage audit. An empty unknown-path
list after a full trigger does not establish that every path has an owner. The API neither rematches
policy nor reads Git/files/environment, runs tests, or infers runtime savings. Preview plans with no
binding identity remain previews; a matching digest does not authenticate the report's origin.

For an already decoded plan, `parse_plan_document(value)` reconstructs a validated `PlannedTests`
and checks the closed version-one fields, canonical ordering, source/selection semantics, and digest.
The caller must bound file reads and reject duplicate JSON keys before decoding. Both parsing and
diagnostic encoding enforce a complete UTF-8 document within 4 MiB including its final newline;
an oversized report fails without truncation. `encode_diagnostics` checks the versioned envelope and
bounded JSON encoding; use `diagnose_selection` to establish its relationship to the plan.

The [historical replay runner](designs/experiments/django-ray-shadow-replay.md#reproduce-the-comparison)
accepts `--diagnostics-output NEW_FILE`. It writes a separate
`test-replay-diagnostics-version: 1` report with per-revision diagnostics, ranked path counts, suite
additions/omissions, and a hash of the complete replay bytes. Every observation must agree with its
exact cohort source, plan digest, comparison, catalog, and frozen policy/registry/adapter identities.
Both outputs must fit their limits before writing. Existing files are never overwritten.
The default replay output format and exit behavior are unchanged; neither output enables execution.

## django-ray shadow pilot

The initial catalog and fixed maps are reviewed against django-ray commit
[`0894832fabe8ab216c186c470548a15339b71e8e`](https://github.com/dariuszpanas/django-ray/tree/0894832fabe8ab216c186c470548a15339b71e8e).
This is source inspection evidence, not a qualification run. The pilot has two independent paths:

1. An advisory PR comparison plans the complete explicit change range and records the existing
   selector's suites, YAGA's suites, additions, omissions, reasons, fallback, and planning overhead.
   It does not alter the existing qualification gate or its job conditions.
2. An explicitly requested full shadow run calls all five existing workflows once at the same
   source. It retains their normal assertions, artifacts, matrix behavior, and cleanup. Its
   concurrency namespace must stay separate from required CI so a pilot cannot cancel it.

The first [historical selection replay](designs/experiments/django-ray-shadow-replay.md) compared
31 merged revisions with the frozen current selector: zero existing obligations were omitted,
but 28 revisions selected the full catalog. This records selection behavior and local planning
overhead only; it did not execute qualifications.

The subsequent [full shadow pilot](https://github.com/dariuszpanas/django-ray/actions/runs/39090724517)
passed all eight qualification cells at django-ray `e4f1ccc0be7944272ebbe82d8524d8a1bd09d8c9`
in 27m 54s, including setup and cleanup. Its
[result summary](designs/experiments/django-ray-full-shadow-result.json) retains exact identities,
timings, artifact digests, and audit facts. This establishes one full-catalog baseline, not savings:
the current selector requested no qualification workflows for that change, while the proposed
policy forced full selection. The [next-phase plan](designs/test-planning.md#pilot-review-and-next-phase-delivery-plan)
addresses dependency/ownership gaps, complete receipt evaluation, controlled comparisons against
current selection, and advisory activation criteria. Required CI remains unchanged.

The consumer implementation lives in `.github/workflows/test-planning-shadow.yml`, with policy
`.yaga/test-planning-shadow.toml` and fixed adapter `scripts/test_planning_shadow.py`. Its explicit
local planning command is:

```bash
uv run python -m scripts.test_planning_shadow plan \
  --repo . --policy .yaga/test-planning-shadow.toml \
  --baseline-policy scripts/qualification_policy.json --range BASE...CANDIDATE \
  --execution-commit EXACT_CANDIDATE_COMMIT --execution-tree EXACT_CANDIDATE_TREE \
  --adapter-commit EXACT_ADAPTER_COMMIT --adapter-tree EXACT_ADAPTER_TREE \
  --output NEW_OUTPUT_DIRECTORY
```

`--full-shadow` additionally emits a full plan; it does not itself run workflows. Keep the adapter
source and candidate execution source distinct and verify both identities. The workflow's manual
mode requires an explicit base commit and executes its selected exact source through the existing
five workflow bindings. Preserve the consumer's own setup and invocation instructions.

The artifacts are `proposed-plan.json`, `proposed-handoff.json`, `executor-manifest.json`, and
`comparison.json`; manual full mode also retains `full-plan.json` and `full-handoff.json`.
Comparison version 1 records source and policy/registry/plan identities, baseline and proposed suite
sets, missing/additional suites, selected cells, effective mode, and measured planning overhead.
`planning_seconds` measures plan creation, handoff, and baseline comparison; it excludes registry
construction, dependency/environment setup, and artifact upload. Runtime measurements remain
unavailable until observed. An always-running pilot aggregate checks
the fixed workflow results; it does not synthesize cell receipts or change the required gate.

Start with at least 30 representative PR revisions across backend, assets, infrastructure,
dependencies, tests/configuration, and mixed changes. Keep incomplete collection, planning errors,
and unavailable results explicit. Differences that select less than the existing policy require
explanation and a regression case before promotion. Full shadow results reveal failures that a
proposed subset would omit; comparison alone cannot establish those outcomes.

Record wall-clock feedback time, runner minutes, queue/setup/cleanup costs, planner overhead,
fallback rate, cancellation, and excluded failures. Do not derive a wall-clock speedup by summing
parallel job durations or counting fewer tests. Use controlled replay or actual selected runs to
measure realized feedback time and order effects.

**Measure first; decide before activation.** No minimum improvement is assumed for this pilot.
After collecting evidence, the owner will set the minimum useful feedback-time reduction and
assess it before selective execution is enabled. Activation also needs no unexplained omissions,
no known missed regressions in the cohort and regression replay, complete-result failure tests,
preserved cleanup/matrix obligations, and a tested full-profile rollback. Until then, required CI
continues unchanged; no pilot artifact authorizes skipping it.
