# Selective test and qualification planning

**Status: phases 1 through 3 and phase 4A diagnostics/example mappings implemented; one full shadow pilot passed.**
Phases 4B through 4D remain proposed. The version-one policy
model, strict loader, pure selection planner, installed CLI, Git source binding, and versioned plan
reports are available.
Both example configurations are exercised by the test suite. See the
[test planning guide](../test-planning.md) for current commands and the
[handoff guide](../test-planning-handoff.md) for fixed executor preflight, runnable mappings, and
the django-ray pilot. Historical selection replay and one full same-source observation are recorded;
actual selective feedback-time measurements and the adoption decision remain pending;
receipt evaluation remains proposed, and no consumer test skipping is enabled.

YAGA should turn an explicit source change and qualification policy into a deterministic list of
work for existing executors. The work can be Python tests, frontend tests, image builds, Docker
checks, Chainsaw scenarios, or a custom qualification lasting more than an hour. Development
profiles may select affected suites. Consolidation and release profiles require the complete
configured inventory before promotion.

The first release should support every execution category through named bindings. Coverage and
language-specific dependency analysis can later improve selection within a suite; they are not
prerequisites for adopting the feature.

## Deliverables and boundaries

- An explicit, versioned TOML policy describing suites, tags, impact rules, and profiles.
- An installed-only `yaga test plan` command with text and versioned JSON output.
- Fixed adapter bindings that let existing local scripts and CI workflows consume selected IDs.
- A later receipt evaluator that verifies every selected suite and environment combination.
- Conservative fallback, selection explanations, and a measured shadow rollout.

The planner does not execute repository code, import plugins, collect tests, discover branches,
fetch Git history, provision infrastructure, schedule jobs, manage credentials, or parse arbitrary
shell commands. Execution, isolation, cancellation, retries, and cleanup stay with the existing
runner. Selection does not establish that two suites can safely run in parallel.

Keep this domain separate from `change check`, which enforces changed-file coupling, and from
repository-check plan v1/v2. Their schemas remain unchanged. The new domain stays outside both
dependency-free composite Action import graphs. This proposal does not change the Agent Review
publisher or add merge-queue support to it; that publisher currently does not support merge queues.

YAGA already has a useful architectural precedent: [Agent review plans and receipts](../agent-review-gate.md)
use named work items, deterministic plan digests, external adapters, and result evaluation. Reuse
that approach without sharing its review-specific schema or outcome rules.

## Model

| Entity | Meaning | Example |
| --- | --- | --- |
| Suite | A named, independently verifiable unit of work; may include an existing build/qualification procedure | `infra.chainsaw` |
| Tag | Catalog metadata for grouping suites; no native runner semantics | `frontend`, `integration`, `long` |
| Variant | A required environment/scenario cell for a suite | `py312`, `jobs-crash` |
| Binding | An opaque ID resolved by a fixed adapter registry | `frontend-browser` |
| Impact rule | Changed path patterns selecting suite IDs and/or tags | `schemas/**` selects backend and frontend |
| Prerequisite | Another suite whose required variants must pass first | `image.build` |
| Capability | An environment requirement the executor must satisfy | `docker`, `kubernetes` |
| Profile | Whether to select affected suites or require the full catalog | `development`, `integration`, `release` |

Suite IDs identify obligations. Tags help select those obligations; they are not executable pytest
expressions, shell fragments, or permission grants. A runner may translate a fixed binding into
pytest markers, frontend projects/tags, Chainsaw selectors, or a reusable workflow.

A variant is an explicit label resolved by the binding. There is no implicit Cartesian product,
host-dependent discovery, or automatic dropping of unavailable variants. In v1, selecting a suite
selects **every** declared variant. Use separate suites when different obligations require separate
selection policies. Pin actual tool versions and image identities in the executor configuration;
a label such as `node-supported` does not itself pin an environment.

Suite-level `needs` means all variants of every prerequisite. Reject cycles, self-dependencies,
and missing IDs. Produce a deterministic topological order with lexicographic ID tie-breaking.
Cell-to-cell dependency mappings are deferred; v1 never guesses that matching variant names imply
compatible artifacts. Artifact compatibility and transport are the binding's responsibility.

## Policy v1

Read exactly one explicitly supplied UTF-8 TOML file. Do not discover it, merge files, or load
includes. The namespace is `test-policy-version`, independent of existing YAGA schemas.

| Location | Keys and semantics |
| --- | --- |
| Root | Required `test-policy-version = 1`, `suites`, `profiles`; optional `unknown-paths`, `full-on`, `no-impact`, `rules` |
| `unknown-paths` | `full` by default, or `error`; no `skip` value |
| `full-on` | Path patterns that force the entire configured catalog; default empty |
| `no-impact` | Patterns acknowledging paths with no additional suite obligation; default empty |
| `[[suites]]` | Required `id`, `binding`; optional `tags`, `variants`, `needs`, `requires` |
| Suite defaults | Empty tags/dependencies/capabilities; variants default to `["default"]` |
| `[profiles.<name>]` | Required `mode` (`selective` or `full`); optional `always` suite IDs, default empty |
| `[[rules]]` | Required unique `id`, nonempty `when-any`; `select` IDs and/or `select-tags`, with a nonempty combined selection |

Require at least one suite and one profile. All keys are closed. Reject wrong types, unsupported
versions, duplicate array entries, duplicate IDs, unknown suite/profile/tag references, empty
variant lists, and rules whose tag expansion is
empty. Tokens are lowercase ASCII matching `[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*`, at most 64 bytes;
no normalization is performed. Catalog order is not selection precedence.

Patterns use YAGA's existing anchored, case-sensitive repository-relative POSIX grammar: literals,
component-local `*`, and whole-component `**`. Other permitted characters are literal: there is no
regex, negation, interpolation, or brace expansion. Policy fields cannot contain commands,
secrets, permissions, or runner arguments. Duplicate patterns across
different rules are allowed and compiled once; duplicates within a list are errors.

Hard bounds for policy validation and planning:

| Input/work | Limit |
| --- | --- |
| Policy file | 128 KiB, bounded regular-file read |
| Catalog | 128 suites, 16 variants per suite, 512 expanded cells total |
| Metadata | 16 tags and 16 capabilities per suite; 128 distinct tags/capabilities each |
| Dependencies | 16 per suite, 512 edges total |
| Profiles/rules/patterns | 16 profiles, 128 rules, 1,024 distinct patterns; 128 entries per pattern list |
| Changed paths | 2,048 canonical paths; 4,096 UTF-8 bytes and 64 components each; 4 MiB total including one delimiter byte per path |
| Pattern work | One shared 10,000,000-unit budget per plan |
| Semantic selection | 4 MiB as compact sorted-key UTF-8 JSON, including inventory and reasons; reject rather than truncate |
| JSON plan | 4 MiB including the complete source envelope; reject rather than truncate |
| Human plan reasons | At most 256 displayed entries with exact omitted counts |
| Later receipt | Proposed 4 MiB including its complete evidence envelope |

The phase-one bounds are locked by tests. Valid
repositories exceeding them receive an explicit operational error; increasing bounds requires
measured review. Never silently reduce a Git range or suite inventory to fit a limit.

### Selection algorithm

1. Strictly validate the entire policy, all references, expanded tags, and the prerequisite DAG.
2. Resolve the explicit source identity and collect the complete changed-path set when requested.
3. For a `full` profile, select every suite and every variant. `always` is redundant but legal.
   Bypass impact/unknown-path classification even when a range was supplied; an unmapped path
   cannot invalidate full selection. Policy validation and complete source validation still apply.
4. For a `selective` profile, start with `always`; evaluate every matching rule and union its suite
   IDs with all suites having any selected tag. Rules are additive, never first-match exclusions.
5. In selective mode, any `full-on` match selects the complete catalog and bypasses unknown-path
   classification and impact-rule evaluation for the whole change. The implementation checks
   these triggers before expanding rule reasons. Otherwise every changed path must match a selection rule
   or `no-impact` pattern. An unmatched path applies `unknown-paths`: `full` selects the catalog;
   `error` produces exit `2` without a plan.
6. Expand prerequisite closure, order deterministically, and expand all required variant cells.
7. Emit the source identities, effective mode, complete obligations, and reasons. Do not run them.

`no-impact` only prevents unknown-path fallback. It cannot undo a rule, a full trigger, or a
mandatory suite. A mixed documentation/code change still selects the code suites. An empty diff
selects `always`; if that is empty, report an explicit zero-obligation plan rather than pretending
tests passed. It may be a valid selective plan, but full profiles always retain the complete catalog.

Changes to fixtures, test files, runner configuration, lockfiles, images, and generated inputs
must be represented in the policy. Unknown paths default to full. Renames appear as deletion plus
addition with the existing rename-disabled reader; consider both names. Deletions, mode changes,
and gitlink changes remain visible, and the planner never traverses submodules.

Malformed/incomplete Git output, shallow range history, missing objects, ambiguous merge bases,
and exceeded budgets produce exit `2` without an executable plan. These differ from a valid,
complete diff containing an unknown path, which can safely request the full configured inventory.
The workflow must block or run its separately defined full path after a planning failure; it must
not interpret missing output as an empty selection.

### What full means

`full` means every suite and every required variant in the supplied catalog, including prerequisites.
It is not a discovery mechanism and does not prove that the catalog covers every repository test.
Repositories must reconcile the catalog with their existing required jobs before using it to skip
anything. Missing Docker, a cluster, credentials, or an interpreter leaves required work blocked.

V1 intentionally has no advisory suites, cost-based omission, duration cutoff, or automatic reduced
environment matrix. A full profile's obligations cannot shrink because the machine is too small.

### Phase-one Python interface

The internal API takes an explicitly loaded policy and a complete tuple of changed paths:

```python
from pathlib import Path

from yaga.test_planning.planner import plan_tests
from yaga.test_planning.policy import load_test_policy

loaded = load_test_policy(Path("docs/designs/examples/test-planning/polyglot.toml"))
selection = plan_tests(
    loaded.policy,
    profile="development",
    changed_paths=("frontend/src/button.ts",),
)
assert tuple(item.suite for item in selection.selected) == (
    "frontend.browser",
    "frontend.unit",
    "repository.smoke",
)
```

`parse_test_policy(raw_bytes)` provides the same strict validation without filesystem access.
The frozen models validate direct construction too. The loader reads one bounded regular file
and retains its resolved path and exact-byte SHA-256; that digest identifies input, not authority.
The pure planner returns immutable `SelectionPlan`, `SelectedSuite`, and `SelectionReason` values
from `test_planning.selection`. It performs no I/O or environment lookup. The full normalized
inventory remains visible even when the selection is empty.

`changed_paths=()` is an explicit empty change. `changed_paths=None` is accepted only by a full
profile, indicating that no comparison was supplied. Invalid paths or duplicate entries fail
even for a full profile. Invalid policy, unknown profile, configured unknown-path errors, and
resource exhaustion raise `ConfigurationError`; invalid path input raises `InputError`.
These errors never return a partial plan. The pure result carries no source identity,
public wire-format version, execution receipt, or claim that tests passed. The installed CLI
adds source-bound reports around this unchanged pure interface.

## Source identity, policy authority, and CLI

Installed commands:

```bash
yaga test policy check --file .yaga/test-policy.toml
yaga test plan --policy .yaga/test-policy.toml --profile development --range BASE...HEAD --format json
yaga test plan --policy .yaga/test-policy.toml --profile integration --revision CANDIDATE --format json
```

Require `--policy`, `--profile`, and exactly one of `--range` or `--revision`. Only `--repo` defaults
to `.`; format defaults to text. `--revision` is legal only with a full profile. No implicit HEAD,
event parsing, branch discovery, fetch, or dirty-worktree selection. `BASE`, `HEAD`, and `CANDIDATE`
above are placeholders for explicit commit-ish arguments; resolve them once to exact object IDs.
Both commands support `--format text|json` and `--quiet`; quiet suppresses successful output in
either format while retaining errors.
Policy checking requires `--file` and does not inspect Git or discover configuration.

Planning also accepts optional `--bindings-sha256 DIGEST`, a lowercase 64-character SHA-256 digest
of an adapter-owned immutable execution manifest. It identifies the binding registry implementation,
its trusted revision, and every declared variant's locked tool/image/configuration definitions.
The planner treats this as opaque identity and never executes or interprets the registry. Include
it in CI plans; its absence is represented as null and permits planning/preview only, not receipt
qualification. Updating a binding's commands or environment definition must update this digest.
Produced image digests are not known before execution: downstream adapters additionally verify
their source/provenance and retain them in the execution evidence.

Reuse the shared Git runtime and the changed-path reader's strict two-/three-dot semantics. A
range plan records both endpoint SHAs and its effective comparison SHA. Its execution commit is
the resolved right endpoint, and it also records that commit's tree. A full revision plan records
the exact execution commit/tree and null comparison fields; it needs the selected objects, not
complete range history. All Git operations preserve the shared no-fetch/replacement/graft controls.

**Execute the right endpoint of the plan.** Testing a PR head does not qualify a synthetic merge.
To test a combined candidate, plan from a suitable explicit baseline to that candidate and execute
that candidate. A three-dot PR diff may be useful for feedback but does not certify interaction
with target-branch changes. Local dirty execution is advisory and cannot produce qualifying receipts.

Trusted CI chooses the profile from its branch/event policy: development updates may be selective;
promotion into a protected consolidation branch and releases use full qualification of the actual
candidate. Scheduling a full run only after promotion explicitly accepts temporary broken integration.
This feature alone does not remove a serial hour-long merge bottleneck; batching, sharding, and
execution capacity remain separate choices.

The policy file is an explicit input, **not proof of authority**. For required CI, load the expected
inventory/profile and binding registry from a reviewed trusted revision. Include policy, binding,
and workflow changes in full triggers. Full testing of a PR-edited catalog alone is insufficient:
a contributor could remove a suite or variant from that catalog.

V1 does not merge baseline and candidate policies. Continue enforcing the trusted policy while
previewing a proposed replacement separately. Newly added suites need explicit candidate runs;
removed/renamed suites, changed bindings, or matrix changes require a reviewed policy migration.
Do not silently union conflicting definitions by ID. Record the trusted policy revision and adapter
revision in CI evidence. PR-controlled plans remain unprivileged quality inputs and cannot authorize
credentials, write permissions, cluster access, or weaker protected-branch requirements.

### Plan and exit contracts

The JSON envelope has exactly `test-plan-version = 1`, a deterministic `plan_digest`, and a
`plan` object. The object contains:

- `test-plan-version = 1`: the semantic version included in the digest.
- `policy_sha256`: SHA-256 of the exact supplied policy bytes; it is identity, not authentication.
- `bindings_sha256`: the expected immutable execution-manifest digest, or null for a preview plan.
- `profile`, `requested_mode`, and `effective_mode` (`selective` or `full`).
- `source`: `base_commit`, `head_commit`, `comparison_commit`, `execution_commit`, `execution_tree`;
  comparison fields are null for a revision-only plan.
- `changed_paths`: sorted complete paths, or null for a revision-only plan.
- `inventory`: the complete normalized suite definitions, including bindings, tags, variants,
  prerequisites, and capabilities, so intentionally unselected suites remain visible.
- `selected`: topologically ordered records with exactly `suite` and `variants`; variant IDs
  are sorted lexicographically.
- `reasons`: records with exactly `code`, `suite`, `rule`, `path`, and `required_by`. All keys are
  present; irrelevant values are null. A prerequisite reason names the added suite in `suite`
  and its dependent in `required_by`. Global full reasons have a null suite. Order records by
  those five fields, with null before strings, and remove exact duplicates.

Reasons explain the performed selection: selective plans record matched rule/path/suite triples,
mandatory suites, acknowledged no-impact paths, fallback causes, and prerequisite edges. Full
profiles require their profile-full reason without computing irrelevant impact rules. A rule/path
pair selecting several suites has a reason for each suite. Human output may summarize reasons
without changing this semantic payload.

Use the existing plan-digest approach: SHA-256 over UTF-8 JSON of `plan`, with sorted object keys,
compact separators, no ASCII escaping, no floating-point values, and canonical array orders.
Include `test-plan-version = 1` inside `plan` as well as the envelope. Timestamps, absolute
local paths, and display formatting do not participate. Exact policy-byte hashing intentionally
invalidates plans after even a comment edit. The CLI emits reports only; importing plans and
validating receipt evidence belong to a later phase.

Stable initial reason codes: `test.always`, `test.rule`, `test.profile-full`, `test.full-trigger`,
`test.unknown-path`, `test.no-impact`, and `test.prerequisite`. Policy/Git/input errors use the existing error envelope;
do not assign success-shaped plan output to them.

Policy check and planning return `0` for a valid result, including full fallback; `2` for invalid or
unavailable inputs and resource errors. They do not use exit `1` or claim tests passed. The later
evaluator returns `0` only for complete success, `1` for well-formed failed/incomplete qualification,
and `2` for invalid evidence or operational errors, with error precedence over findings.

## Complete configuration examples

Download [polyglot.toml](examples/test-planning/polyglot.toml) for an illustrative application with
backend, frontend, Docker, Chainsaw, and custom Ray qualification. All entries use the same schema;
there is no language enum. Paths and binding names must be adapted to the consumer repository.
The installed CLI and internal pure planner accept this fixture. Execution bindings remain examples.

Its important rules include:

```toml
[[rules]]
id = "api-contract"
when-any = ["api/**", "schemas/**"]
select-tags = ["backend", "frontend"]
select = ["container.smoke"]

[[rules]]
id = "infrastructure"
when-any = ["deploy/**", "helm/**", "tests/chainsaw/**"]
select = ["infra.chainsaw"]

[profiles.development]
mode = "selective"
always = ["repository.smoke"]

[profiles.integration]
mode = "full"
```

Expected development-profile behavior for that complete file:

| Changed paths | Selected suites, before variant expansion |
| --- | --- |
| `frontend/src/button.ts` | `repository.smoke`, `frontend.unit`, `frontend.browser` |
| `backend/src/billing.py` | `repository.smoke`, `backend.unit`, `backend.integration` |
| `backend/src/runtime/worker.py` | Previous backend set plus `qualification.ray`, `image.build` |
| `schemas/api.json` | Smoke, both backend suites, both frontend suites, `image.build`, `container.smoke` |
| `deploy/service.yaml` | `repository.smoke`, `image.build`, `infra.chainsaw` |
| `tests/container/test_startup.py` | Smoke, `image.build`, `container.smoke`, `infra.chainsaw`, `qualification.ray` |
| `README.md` plus `frontend/src/button.ts` | Smoke, `docs.check`, both frontend suites |
| `LICENSE` or an empty diff | `repository.smoke` |
| `frontend/package-lock.json` | Full catalog, despite also matching the frontend rule |
| `new-component/source.rs` | Full catalog because the path is unknown |
| Any valid revision with integration/release profile | All 10 suites and all 11 required variant cells |

### Fixed execution bindings

The following are adapter examples, not additional fields accepted in policy v1. Commands execute
only inside the corresponding reviewed CI job/local wrapper; never interpolate an arbitrary
policy value as shell, workflow YAML, or an executable path.

| Binding | Illustrative execution | Completion responsibility |
| --- | --- | --- |
| `repository-smoke` | Existing inexpensive repository checks | All configured checks complete |
| `docs-check` | Existing documentation build/link-check target | Build and link checks succeed |
| `backend-unit` | `uv run pytest backend/tests/unit` in each declared Python environment | Complete collection and process result per variant |
| `backend-integration` | `uv run pytest backend/tests/integration` with an owned PostgreSQL fixture | Tests, fixture isolation, and cleanup |
| `frontend-unit` | `npm ci`, then `npm run test:unit -- --run` in `frontend/`, assuming that script wraps Vitest | Lockfile installation plus non-watch test completion |
| `frontend-browser` | `npm ci`, browser setup, then `npx playwright test --project=chromium` in `frontend/` | Browser/server lifecycle and complete result |
| `image-build` | Existing Docker image build job | Candidate source and produced image digest recorded |
| `container-smoke` | Owned Compose project with `docker compose ... up --exit-code-from test` | Selected service outcome, cancellation state, and owned cleanup |
| `infra-chainsaw` | Owned cluster setup, load the selected image, then `chainsaw test --selector component=backend tests/chainsaw` | Assertions plus cluster/namespace cleanup evidence |
| `ray-qualification` | Existing custom qualification wrapper/workflow | Its complete acceptance receipt and cleanup requirements |

The paths, npm scripts, browser project, Compose service, and Chainsaw labels above are illustrative.
Adapters explicitly map variant IDs to locked interpreters/images/configurations. `needs` provides
ordering obligations, not shared filesystems: downstream bindings verify the exact upstream image
digest and candidate identity before use. A binding may execute all its variants in one existing
workflow and return one result per cell; do not launch a complete workflow once per cell.

Chainsaw [label selectors](https://kyverno.github.io/chainsaw/latest/configuration/options/label-selectors/)
are CLI inputs. Playwright supports [test tags](https://playwright.dev/docs/test-annotations#tag-tests),
and Vitest provides a [non-watch run mode](https://vitest.dev/guide/cli.html#vitest-run). These can refine
an explicitly designed binding; a YAGA tag has no automatic meaning to those runners.
Docker Compose's [exit-code-from option](https://docs.docker.com/reference/cli/docker/compose/up/)
does not replace cancellation and cleanup checks. All external tools remain pinned by the consumer.

## django-ray migration example

The example is grounded in upstream django-ray commit
[`0894832fabe8ab216c186c470548a15339b71e8e`](https://github.com/dariuszpanas/django-ray/tree/0894832fabe8ab216c186c470548a15339b71e8e),
fetched on 2026-10-17. This is a source snapshot, not a new execution or qualification result.

django-ray already has a [path-to-qualification policy](https://github.com/dariuszpanas/django-ray/blob/0894832fabe8ab216c186c470548a15339b71e8e/scripts/qualification_policy.json),
a selector, and an [always-running qualification aggregate](https://github.com/dariuszpanas/django-ray/blob/0894832fabe8ab216c186c470548a15339b71e8e/.github/workflows/qualification-gate.yml).
The shadow pilot preserves that existing arrangement. The
[django-ray.toml](examples/test-planning/django-ray.toml) preserves the five existing long
qualification groups and their path triggers, adding full fallback and broad policy/tooling triggers.
It is intentionally a conservative superset, not a promise of selection parity or a drop-in replacement.

This catalog covers only the five long qualification workflows. Existing ordinary CI remains
mandatory independently; `full` for this file does not mean all django-ray validation has completed.

| Suite | Binding to existing reusable workflow | Required variants | Current job timeout, not measured runtime |
| --- | --- | --- | --- |
| `qualification.application` | `application-qualification.yml` | `default` | 75 minutes |
| `qualification.native` | `upgrade-native-qualification.yml` | `native`, `jobs`, `jobs-crash`, `core-loss` | 60 minutes per cell; existing serial matrix |
| `qualification.latency` | `latency-qualification.yml` | `default` | 45 minutes |
| `qualification.cancellation` | `cancellation-qualification.yml` | `default` | 40 minutes |
| `qualification.monitoring` | `monitoring-qualification.yml` | `default` | 85 minutes |

The workflow bindings retain existing setup, execution, timeouts, artifacts, and cleanup. In
particular, application qualification already calls
[`qualification.application.run_chainsaw`](https://github.com/dariuszpanas/django-ray/blob/0894832fabe8ab216c186c470548a15339b71e8e/qualification/application/run_chainsaw.py).
Preserve its acceptance intent, image/source identity, complete assertion evidence, cold-generation
linkage, and owned cleanup checks. A diagnostic run or plain Chainsaw exit code cannot replace it.
Native qualification requires all four scenarios. Monitoring and cancellation retain their own
cleanup checks; a successful test followed by failed cleanup is not successful qualification.

If ordinary backend suites are added to the catalog later, verified bindings include `uv run make
test-unit`, `test-integration`, `test-postgres`, and `test-testproject` from the snapshot's
[Makefile](https://github.com/dariuszpanas/django-ray/blob/0894832fabe8ab216c186c470548a15339b71e8e/Makefile).
Those targets require Linux. Directory names do not prove hermeticity or disjoint selections.
The existing [test taxonomy](https://github.com/dariuszpanas/django-ray/blob/0894832fabe8ab216c186c470548a15339b71e8e/.github/test-suite-taxonomy.json)
and inventory runner offer finer, explicitly checked groups; preserve serial/resource constraints
and prohibitions on empty collections or inappropriate skips.

Keep local deployment qualification separate. Its `uv run make k8s-final-gate` entrypoint in
[mk/k8s.mk](https://github.com/dariuszpanas/django-ray/blob/0894832fabe8ab216c186c470548a15339b71e8e/mk/k8s.mk)
requires explicit actual cluster context, namespace, restart policy, and applicable service URLs.
It intentionally preserves namespace, PostgreSQL, PVCs, and Docker data. Its restoration/evidence
contract cannot be replaced with disposable-cluster deletion. It is not an automatic PR job in this
example catalog.

Migration sequence:

1. Export an immutable snapshot of the existing policy and workflow bindings; inventory every cell.
2. Compare YAGA and the existing selector on the same complete changes, including historical PRs,
   renamed/deleted paths, and policy/test changes. Record every extra or missing selected suite.
3. Keep existing CI authoritative while running full same-source shadow qualification on a bounded
   cohort. A difference selecting less than the current policy blocks migration until explained.
4. Introduce the YAGA planner while preserving the current workflow bindings and required aggregate.
   Validate failure/cancellation/missing-output paths before permitting any skip.
5. Refine broad rules only with observed evidence and regression cases. Do not simultaneously
   change concurrency, cleanup, environment matrices, and selection.

## CI execution and completion

Use one unprivileged planning job with complete explicit source inputs. Existing fixed jobs consume
its outputs through a reviewed ID-to-binding map. GitHub supports
[passing outputs between jobs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/pass-job-outputs);
the adapter should expose bounded booleans or a validated JSON matrix, not generated shell commands.

The required aggregate runs even after failure or cancellation when the platform permits it. It
verifies planning succeeded, each selected binding ran, all required variants passed, and no
selected job was merely skipped. If cancellation prevents the aggregate from running, branch
protection must leave qualification unsatisfied. An unselected suite is explicitly not required
by this plan; it is never reported as a passed test.

Before the generic receipt evaluator exists, the adapter's aggregate owns these checks. Never
activate selective execution with only conditional jobs and no complete-result aggregate. A
missing binding or capability fails preflight or leaves an explicit blocked obligation.

### Proposed receipt evaluation phase

Add the following command after the planner and adapter contract are established:

```bash
yaga test evaluate --plan PLAN.json --results RESULTS.json --run-id RUN --attempt 1 --format json
```

The expected plan must come from the trusted planner or be
recomputed there; accepting an arbitrary PR-supplied plan and matching receipt proves nothing.

The result envelope is independently versioned with `test-results-version = 1`, matching
`plan_digest` and non-null `bindings_sha256`, exact `execution_commit`/`execution_tree`, `run_id`,
`attempt`, and an array of results. Require `run_id` to match
`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` and `attempt` to be an integer from 1 through 2,147,483,647
(not a boolean). The adapter supplies the expected run ID and aggregate execution attempt as
explicit evaluator arguments from trusted CI context; both must match the envelope. A new overall
CI attempt requires a new complete envelope. Each result names `suite`, `variant`, `binding`, and
an `outcome` from `passed`,
`failed`, `pending`, `cancelled`, or `blocked`, plus optional bounded `summary` and `evidence_sha256`.
The adapter authenticates workflow/job identities, tool/environment fingerprints, and artifact
provenance before producing these records. YAGA validates the handoff; it does not authenticate CI
merely because a digest appears in JSON. Reject preview plans with null binding identity. The
adapter must retain the immutable execution manifest and verify that the jobs actually used it;
copying its digest without that verification does not establish execution identity.

Each selected suite/variant must occur once; reject unknown/duplicate cells, extra unselected
results, invalid digests, mismatched source/binding/run identities, and conflicting aggregate
attempts with exit
`2`. Missing cells and well-formed non-passed outcomes leave qualification incomplete/failed with
exit `1`. A failed cleanup, prohibited native test skip, or incomplete custom acceptance receipt
must prevent the adapter from emitting `passed`. Missing evidence files are operational failures,
not fabricated failure receipts. No cross-revision reuse or arbitrary mixing of green cells from
different runs or aggregate attempts is supported initially. Internal job retries may remain
within one aggregate attempt, but the adapter selects the final authorized job attempt per cell
and retains its complete retry lineage in evidence; evaluation never picks the greenest record.

Use `test.required-result` for missing/pending/blocked obligations, `test.failed` for failed work,
and `test.cancelled` for cancellation. Keep detailed logs and custom receipts in bounded artifacts;
the generic result file carries only their digest and a summary. Matching a digest provides
correlation, not a signature, trusted origin, or proof that a custom verifier was correct.

## Phased implementation plan

Each phase should be delivered as focused Conventional Commit changes, with review/CI fixes folded
into their owning logical commits. Every implementation push runs the existing `uv run make ci`.
Do not weaken current required checks as part of adding the new feature.

| Phase | Scope and likely modules | Acceptance criteria / activation boundary |
| --- | --- | --- |
| 0: design fixtures | This proposal, complete TOML examples, expected selection cases | Review schema, trust split, inventory semantics, and examples; no runtime behavior changes |
| 1: policy and pure planning (implemented) | `src/yaga/test_planning/{models,policy,selection,planner}.py` and shared `src/yaga/path_patterns.py`; focused parser/selection tests | Closed schema and limits; additive tag/rule selection; unknown/full behavior; DAG closure; both examples load and produce tested selections; no executors |
| 2: installed CLI and Git (implemented) | `src/yaga/commands/test.py`; `test_planning/{source,git,service,reports,reporting}.py`; shared `git/changes.py` | Exact source identities, stable JSON/digest and exits, bounded reports, fresh installed-wheel smoke, unchanged Action graphs; useful local/CI planning only |
| 3: executor handoff and shadow pilot (implemented; one full observation passed) | `test_planning/handoff.py`, runnable fixed job maps, installed-wheel smoke, django-ray advisory comparison and manual full-shadow adapter | All five execution categories demonstrated; 31-revision comparison and one full same-source run recorded; no measured selective benefit or activation claim; existing gates remain authoritative |
| 4: selection quality, receipts, and measured advisory feedback (proposed) | `test_planning/{results,evaluation}.py`; evaluate command; adapter conformance, selection audit, and experiment reports | Correct dependency/ownership mappings; exact selected cells/attempt/source correlation; controlled feedback measurements and held-out validation; owner adoption decision; required CI remains unchanged |
| 5: optional impact providers | Independently versioned evidence format and optional collectors for Python/JS/build systems | Whole-suite fallback on absent/incompatible evidence; known-regression replay and seeded dependency changes; no removal of mandatory/full-profile obligations |

Implementation dependencies are linear through phase 3. Receipt format work can proceed during
shadow observation, but skipping does not activate before an equivalent complete-result gate is
verified. Phase 5 is optional and does not delay language-neutral v1. A native execution scheduler,
remote cache service, arbitrary plugin system, and code-generating policy language are separate
future proposals, not hidden tasks in these phases.

### Reuse and test strategy

Use `src/yaga/git/` for all Git execution. Extract any genuinely shared changed-path/glob helpers
into a neutral internal module with characterization tests; do not import private coupling-policy
internals or duplicate subprocess logic. Follow existing explicit-config, bounded-read, sanitized
reporting, and UTF-8 entrypoint patterns. Reuse digest conventions, not Agent Review domain types.

Required implementation checks:

- Parser cases for every key/type/bound, duplicate tokens, unknown references, impossible tag
  selections, dependency cycles, and oversized policy/report data.
- Pure selection fixtures for frontend-only, backend-only, cross-stack API, Docker, Chainsaw,
  custom qualifications, mixed docs/code, empty changes, unknown paths, full triggers overlapping
  selective/no-impact rules, and complete variants/prerequisites.
- Real Git tests for two-/three-dot ranges, stale/missing objects, ambiguous merge bases, shallow
  ranges, added/deleted/renamed files, mode/gitlink changes, and malicious/oversized path output.
- Equivalent policy arrangements produce the same normalized selection/order. Identical policy
  bytes, binding identity, and source inputs produce identical digests across paths/platforms and
  repeat runs; comment/order edits intentionally change the policy hash. Preserve existing provider
  results and Action import boundaries.
- Adapter contract tests where selected work is missing, cancelled, blocked, unexpectedly skipped,
  run against another tree, missing one matrix cell, or fails cleanup after assertions pass.
- Installed-wheel CLI qualification outside the checkout, documentation/link builds, and the full
  existing Python/OS CI matrix before publishing the implementation.

### Shadow pilot and promotion criteria

Start with an initial cohort of at least 30 representative PR revisions, including backend,
frontend/assets, infrastructure, dependencies, test/configuration changes, and cross-component
changes. This is a sampling target, not a statistical guarantee. For django-ray, preserve the five
existing workflow bindings and their current selection results throughout the comparison.

Record planned suites/cells, selection reasons, fallback frequency, collection/planning overhead,
runner minutes, wall-clock critical path, setup/cleanup costs, cancellations, and failures that the
proposed subset would have excluded. Full shadow runs reveal omitted failures; actual selected
runs or controlled replay are still needed to measure realized speed and order-dependent effects.
Do not estimate speedup solely from test counts or sum parallel durations as wall time.

Activation requires: no unexplained omissions versus existing mandatory selection; no known missed
regressions in the cohort and regression replay; demonstrated invalid-plan/missing-result blocking;
preserved cleanup and matrix requirements; and a documented improvement in measured feedback time
after overhead. Measure first; the repository owner sets the minimum useful improvement after
observing the pilot and before activation. Do not invent a threshold or claim savings from
selection counts.
Any discovered missed regression adds a regression case and broadens affected rules before further
activation. A single trusted switch to the full profile provides rollback without editing rules.

Continue scheduled full audits after activation. Passing a finite cohort does not prove absence of
false negatives, so consolidated/release full qualification remains mandatory under its policy.

## Pilot review and next-phase delivery plan

This section records the phase-three result and proposes implementation work. It does not change
the policy, dispatch experiments, enable skipping, or approve a numeric adoption threshold.

### Observed baseline

The [full shadow run](https://github.com/dariuszpanas/django-ray/actions/runs/39090724517)
completed on 2026-10-17 in America/Los_Angeles, attempt 1, at django-ray commit
`e4f1ccc0be7944272ebbe82d8524d8a1bd09d8c9`, tree
`272fde67c04c975e35d2bb3f09207fbfb67c357b`. It used YAGA
`e359bd13d26516231585f7dba4d8f509d1d66989` and explicit comparison base
`34ef6d2f400fbc7aa59994b7cdfc15b31fd4479e`. The
[portable result summary](experiments/django-ray-full-shadow-result.json) retains those identities,
job timings, artifact IDs/digests, and raw-byte hashes for the original `INDEX.json` and
`acquisition-index.json` evidence inventories.

| Observation | Result | Interpretation |
| --- | --- | --- |
| Qualification cells | 8/8 passed across five workflows | Includes all four serial native variants |
| Planning and aggregate | Both passed; 10 total successful jobs | No reruns |
| Receipt checks | 83 passed; no missing/invalid evidence | Original archives, source identities, and cleanup independently reviewed |
| Full-shadow elapsed | 27m 54s | Run creation through final job completion, including setup and cleanup |
| Application job elapsed | 27m 27s | Dominated this run's completion time |
| Native matrix span | 24m 44s | Serial matrix span; not four parallel jobs |
| Summed job elapsed | 4,001 seconds | Resource observation, not elapsed feedback time or billing |
| Planner / planning job | 0.032225 seconds / 12 seconds | Planner time alone excludes workflow overhead |
| Current selector / proposed selector | Empty / full catalog for this change | This forced-full pilot is not a performance comparison against current CI |

Required `Commit Messages`, `CI Gate`, and `Qualification Gate` remained active; no branch-rule
changes were made. The observation covers this catalog's bounded application/native acceptance,
not every release, data-upgrade, transaction, or deployed-environment obligation. Later django-ray
runtime changes need new source-bound evidence. Keep the original pilot immutable.

The [historical replay](experiments/django-ray-shadow-replay.md) contains 31 revisions: 28 full
selections and three empty selections, with no partial selections. Its 17 explicit full triggers
partition into 13 workflow-touch changes, three dependency-only changes, and one Makefile/taxonomy
change. The other 11 full selections came from unknown paths: 28 occurrences, of which 26 were
tests and two were `scripts/verify_wheel.py`.

The existing selector requested 50 workflow dispatches across that cohort; YAGA requested 140.
Zero omitted baseline obligations demonstrates a conservative superset, not complete dependency
coverage or zero missed regressions. The replay and live pilot used unchanged policy content, but
their exact policy-byte hashes differ because the Windows replay read CRLF checkout bytes and the
Linux pilot read LF bytes. Preserve both recorded identities rather than normalizing old evidence.

### First selection changes to review

1. **Correct shared dependencies before narrowing.** At the pilot source,
   [latency's scenario](https://github.com/dariuszpanas/django-ray/blob/e4f1ccc0be7944272ebbe82d8524d8a1bd09d8c9/qualification/latency/scenario.py#L11)
   imports and calls `qualification/docker/scenario.py`. Both current selectors omit latency for
   an isolated change to that helper; the pure YAGA planner confirms only native and cancellation.
   Phase 4A adds explicit latency ownership and a regression fixture to the YAGA example.
   Review other shared helpers,
   fixtures, environment inputs, and image dependencies for the same gap. This is a demonstrated
   dependency-map gap, not an observed missed failing qualification. The django-ray 0.7 hardening
   work owns this additive consumer repair and its regression coverage. Phase 4 must verify the
   merged correction at its selected source rather than duplicate that pending implementation.
2. **Assign exact test ownership.** Phase 4A maps these application qualification tests to explicit
   application selection: `test_application_chainsaw.py`, `test_application_core.py`,
   `test_application_generation.py`, `test_application_result_presentation.py`,
   `test_application_runtime_env_refusal.py`, `test_application_profile.py`,
   `test_application_api_runner.py`, and `test_application_workflow_runner.py`, all under
   `tests/unit/`. Do not blanket-exclude `tests/**` or `scripts/**`.
3. **Keep uncertainty conservative.** Retain unknown-path full fallback, policy/registry/gate
   full triggers, shared dependency/lockfile triggers, complete native variants, and current
   acceptance/cleanup behavior. Review wheel-verifier and workflow ownership separately; a
   green replay alone cannot justify treating them as no-impact.
4. **Only then examine granularity.** Keep the five current qualification bindings intact first.
   If application remains on the critical path, add independently verifiable fast backend or
   frontend feedback suites using real existing test targets and the checked taxonomy. A browser
   or Chainsaw slice needs its own acceptance, fixtures, prerequisites, and receipts before it
   becomes an independent suite. Do not relabel a partial application run as full acceptance or
   split native variants into four invocations of the existing complete matrix. Coverage/import
   evidence can suggest mappings later; it is not permission to omit obligations.

An in-memory pure-planner replay adding only those eight application-test paths changes PRs
625, 619, 623, 614, and 595 from five selected workflows to application alone. It reduces the
cohort from 28 to 23 full selections and from 140 to 120 dispatches. That initial counterfactual
changed no policy file or executor and ran no qualification. Phase 4A now includes these exact
mappings and complete-path regression cases in the example policy; original replay observations
remain unchanged. This selection-only result still exceeds the current
selector's 50 dispatches and retains the expensive application job: it establishes neither a
safe migration nor feedback-time savings.

Regression fixtures must include those five exact ranges, the shared Docker helper, frontend
assets with their tests, shared fixtures, renamed/deleted/new paths, mixed application tests plus
`uv.lock`, and all explicit full triggers. Keep policy tuning on this discovery cohort; reserve
fresh revisions for validation.

### Focused implementation sequence

| Delivery | Ownership and scope | Exit condition |
| --- | --- | --- |
| 4A: selection diagnostics and ownership corrections | Pure YAGA diagnostics API and optional replay report; exact application-test mappings and regression fixtures; coordinated django-ray release policy changes | Correct shared-helper selection, reviewed exact ownership mappings, unchanged unknown/full semantics, both selectors compared on complete paths |
| 4B: result evaluation and measurement contract | YAGA pure versioned result/evaluation model and installed `test evaluate`; django-ray fixed receipt normalizers and independent experiment report | All selected cells and prerequisites verified against expected source/policy/manifest/run/attempt; malformed, stale, missing, skipped, cancelled, and failed-cleanup cases block success |
| 4C: bounded selected-versus-current experiments | django-ray manual, non-required adapter; fixed dispatch map, isolated resource/concurrency identities, immutable plans and original receipts | Actual selected execution measured against same-source current selection and a full-catalog failure oracle; required jobs never cancelled or replaced; capacity isolation or measured interference limits |
| 4D: held-out evaluation and adoption decision | Reviewed report and explicit owner decision; trusted full-profile rollback | All safety criteria pass and measured benefit satisfies the threshold fixed before confirmation; otherwise remain in shadow mode |

Phase 4A is implemented by the [separate diagnostics contract](../test-planning-handoff.md#explain-selection-without-changing-the-plan)
and example regression tests. It distinguishes all-suite breadth from a full-mode decision,
preserves paths bypassed by early full selection, and reports baseline availability explicitly.
Diagnostics never rematch policy or assert complete ownership from an absence of unknown reasons.
The django-ray release work owns the matching consumer policy, baseline shared-helper repair,
and consumer regression tests as a separate logical change; verify its exact committed source
before a new shadow run. The example also mirrors that work's additive lease dependencies:
`src/django_ray/lease_policy.py` selects application, native, cancellation, and monitoring;
`src/django_ray/management/commands/django_ray_lease_policy.py` selects application and native.
No required workflow conditions, qualification variants, receipt checks,
or cleanup behavior are removed. Phases 4B–4D remain proposed; phase 4A dispatches no qualifications
and supplies no execution-activation authority.

Deliver these as separate reviewable changes; do not combine policy narrowing with scheduler,
concurrency, cleanup, or environment-matrix changes. Keep result evaluation installed-only and
pure, outside both Action graphs. Use the proposed receipt contract above: complete bounded
documents, independent expected identities, no arbitrary command or credential inputs, and no
cross-source or cross-attempt assembly of successful cells. Adapter normalizers validate each
custom assertion/cleanup contract before emitting `passed`; an aggregate green badge is not
enough. The pilot's retained audit informs fixtures but is not a production receipt verifier.

Version experiment observations separately from frozen plan/handoff/comparison v1 documents.
Include exact base/head/comparison/execution identities, policy bytes, manifest, adapter revision,
run/attempt, environment/image/dependency fingerprints, cache state, cell outcomes, artifact
digests, timing availability, retries, and incomplete observations. Bound input/output and reject
non-finite or inconsistent durations. Do not turn the pure planner into a CI API client or runner.

### Measurement protocol

The primary benefit under unchanged required checks is **earlier advisory feedback**. A faster
subset does not make a PR merge-ready before its required checks finish. Running an extra lane
also adds resource use; report total CI resource change, not just the selected lane's smaller cost.

For every exact source, record three plans: current authoritative selection, proposed selection,
and full catalog. The current selector is the benefit comparator; full execution supplies an
additional failure oracle. Reuse one arm when its complete cells, definitions, and acceptance
match another. When current and proposed differ from full and each other, measure a current arm
as well, or explicitly limit the claim to comparison against forced full. Never substitute an
old run at another source or a sum of excluded job times for a measured comparison.

Proposed discovery budget: eight distinct source pairs, at most 16 full/selected executions and
eight additional current-selector executions when needed. Report the expected runner budget
before dispatch, allow only one pair at a time, and stop when that budget is exhausted. Reuse
compatible existing evidence for correctness where possible; it is a timing comparator only when
the same-source measurement protocol also matches. This plan does not authorize those runs now.

Predeclare balanced order across every distinct arm, including the current-selector arm when it
differs, for preselected backend, frontend/assets, infrastructure, qualification-test, dependency,
and mixed changes. Do not always run the current arm last. Include cases that correctly
remain full. Pin environments and caches, use fresh isolated fixtures, preserve the serial native
matrix, and separate experiment/arm resource and concurrency identities from required CI. Do not
run selected qualifications on every PR while validating this approach. Prefer reserved capacity
isolated from required jobs. Otherwise admit experiments only within a declared spare-capacity
budget: a required run starting or a capacity conflict stops admission of the next pair. That
cannot guarantee zero delay from an already-running pair. Retain observed interference and
deferred/aborted results, stop further experiments when the declared interference limit is reached,
and do not silently discard affected pairs from the report.

| Metric | Definition and reporting |
| --- | --- |
| Validated advisory workflow feedback | Every arm uses GitHub workflow `created_at` through complete selected-result aggregate completion; include scheduling within the run, planning, setup, assertions, cleanup, and publication; report dispatch-to-creation separately when available |
| End-to-end developer feedback | Trusted candidate-ready event receipt through validated result publication; never use Git author dates; unavailable unless the same event timing is collected for both alternatives |
| First actionable failure | Time until an observed test failure becomes available; record infrastructure failures separately and final cleanup completion too; all-green means unavailable, not zero |
| Required-CI readiness | Same candidate's existing required-gate completion; report separately and make no reduction claim from this advisory rollout |
| Scheduling and critical path | Observed job/dependency span; separate queue, dependency wait, and native serial wait when available, otherwise mark unknown |
| Execution and overhead | Per-job setup/assertion/cleanup, planner time and whole planning-job time, runner image/class and cache state |
| Resource cost | Summed observed runner job-seconds including retries and extra pilot jobs; billing remains unavailable without billing evidence |
| Selection quality | Full/unknown fallback rates, selected/extra/missing cells, failure-bearing excluded cells, and unclassified paths by ownership |

For each pair report both absolute seconds and relative change against current selection, with
per-category distributions and all individual observations. Retain failures, cancellations,
retries, and incomplete pairs; do not silently discard slow runs or choose the fastest retry.
Ratios with zero baseline or unavailable timing are unavailable. Do not claim reliable tail
percentiles from eight pairs; report the range and uncertainty. Deliberate waiting between serial
experiment arms is not production route latency; end-to-end claims need comparable candidate-event
observations from a later bounded advisory canary with required CI still enabled.

After discovery, the owner chooses the feedback metric, minimum useful absolute/relative
improvement, acceptable tail regression, and additional resource budget. A workflow-only result
does not establish an end-to-end developer improvement. Freeze these, the policy, environment, sampling
rules, and analysis before confirmation. Validate on at least 20 fresh revisions across the
categories, including at least eight new controlled timing pairs and known-regression/seeded
failure cases. These are sampling floors, not a statistical guarantee; fix an execution budget
before dispatch and return inconclusive if the evidence cannot support the chosen criterion.
Any policy change following a miss needs fresh held-out confirmation.

### Activation criteria and rollback

All criteria below are required for an opt-in **advisory developer lane**. No date, green run count,
or selector-only replay automatically enables it.

- Every selected cell has valid source, policy, binding, run/attempt, acceptance, and cleanup
  evidence. Wrong/stale identity, missing/duplicate/unknown cells, unavailable capability,
  cancellation, prohibited skips, and cleanup failure cannot produce advisory success.
- No unexplained omission of current mandatory obligations or known shared dependencies. A
  full-oracle regression must fail its intended assertion and the candidate selection must
  include and detect it. An unexercised mutant does not count as a caught regression. Require zero
  observed misses in the held-out and known-regression cases, without claiming zero possible risk.
- Measured net feedback benefit meets the owner's predeclared confirmation threshold after all
  overhead; tail behavior and total added resource use meet their declared budgets. If application
  or native qualification still determines completion, report that limitation and decline an
  unsupported speedup claim.
- Full-profile rollback is exercised, including malformed plan, missing result, and cancellation
  paths. A trusted switch restores the complete catalog without editing impact rules. Planning
  failure blocks the advisory result or takes a separately verified full path; it never means
  empty success. Define continued full-audit sampling and immediate rollback on an unexplained miss.
- `Commit Messages`, `CI Gate`, and `Qualification Gate` remain enabled with their existing
  conditions and bodies; integration/release full policies remain authoritative. Advisory
  completion uses a distinct name and cannot substitute for any required status.

Replacing required job selection or reducing a required qualification is a separate future
authorization and migration, even if the status name remains present. It is outside this phase.

## Later automatic impact information

Coverage is observed execution, not a complete dependency proof.
[Pytest coverage contexts](https://pytest-cov.readthedocs.io/en/latest/contexts.html) can attribute
execution to individual test IDs; frontend/build-system adapters may provide other dependency
evidence. Keep collectors optional and external to the deterministic planning core.

A future evidence artifact must bind source commit/tree, test inventory, policy and collector
versions, environment/dependency fingerprints, and completeness. Selection must account for every
change since that evidence's baseline, not just the latest PR diff if the baseline predates it.
Incomplete subprocess/native observation, import/fixture effects, dynamic dispatch, non-code
assets, new tests, and stale identities require broader suite selection. Mandatory manual rules
and full profiles remain authoritative; automatic evidence may refine only explicitly eligible
suites and must never silently override those obligations.

Evaluate existing collectors before building new ones. Freeze their adapter contracts separately
after measured prototypes; do not reserve unrestricted plugin/configuration escape hatches in v1.
