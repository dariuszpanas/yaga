# Selective test and qualification planning

**Status: phases 1 and 2 implemented; phase 3 historical replay recorded, full shadow runs pending.** The version-one policy
model, strict loader, pure selection planner, installed CLI, Git source binding, and versioned plan
reports are available.
Both example configurations are exercised by the test suite. See the
[test planning guide](../test-planning.md) for current commands and the
[handoff guide](../test-planning-handoff.md) for fixed executor preflight, runnable mappings, and
the django-ray pilot. Historical selection replay is recorded; full same-source pilot observations
and the adoption decision remain pending;
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
| 3: executor handoff and shadow pilot (historical replay recorded; full shadow runs pending) | `test_planning/handoff.py`, runnable fixed job maps, installed-wheel smoke, django-ray advisory comparison and manual full-shadow adapter | All five execution categories demonstrated; planning/selection failures cannot pass; django-ray comparison and full same-source shadow runs required; record observed results before claiming savings; existing gates remain authoritative |
| 4: receipts and controlled activation | `test_planning/{results,evaluation}.py`; evaluate command and adapter conformance fixtures | Exact selected cells/attempt/source correlation; malformed/stale/missing/cancelled/cleanup failures tested; activate only after shadow criteria and required gates are verified |
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
