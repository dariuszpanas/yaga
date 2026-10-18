# Test planning

`yaga test plan` describes which suites and required variants a change needs. A suite can represent
backend or frontend tests, a Docker build/check, Chainsaw scenarios, or a custom qualification.
The command reads an explicit policy, compares explicit Git sources, and emits a reproducible plan.
It does not run tests, schedule jobs, or enable CI skips.

Use a selective profile for development feedback and a full profile for consolidation or release
qualification. `full` means every suite and variant in the supplied catalog; it does not discover
tests outside that catalog. Existing required checks remain authoritative while you evaluate plans.

## Validate a policy

```bash
yaga test policy check --file .yaga/test-policy.toml
yaga test policy check --file .yaga/test-policy.toml --format json
```

The file is required and is independent of `.yaga.toml` commit policy and repository-check plans.
There is no discovery, merging, include mechanism, or environment interpolation. Validation performs
no Git access and can run outside a repository. The entire catalog is validated, including unused
suites and rules, before any plan is produced.

Start with this small policy, adapting paths and binding IDs to your repository:

```toml
test-policy-version = 1
unknown-paths = "full"
full-on = [".yaga/test-policy.toml", ".github/workflows/**", "uv.lock", "frontend/package-lock.json"]
no-impact = ["LICENSE"]

[[suites]]
id = "backend"
binding = "backend-unit"
variants = ["py312", "py315"]

[[suites]]
id = "frontend"
binding = "frontend-unit"
variants = ["node-supported"]

[profiles.development]
mode = "selective"

[profiles.integration]
mode = "full"

[[rules]]
id = "backend"
when-any = ["backend/**"]
select = ["backend"]

[[rules]]
id = "frontend"
when-any = ["frontend/**"]
select = ["frontend"]
```

Bindings are opaque IDs for a fixed executor mapping. They contain no commands or runner
arguments, and YAGA never executes them. A variant names a required environment or scenario:
selecting `backend` above selects both Python variants. Names alone do not pin tool versions.

See the complete tested [polyglot configuration](designs/examples/test-planning/polyglot.toml)
for backend, frontend, Docker, Chainsaw, and custom qualification suites with prerequisites.
The [django-ray configuration](designs/examples/test-planning/django-ray.toml) models five existing
long-running qualification groups; ordinary django-ray CI remains mandatory separately. These are
adaptable examples, not ready-made catalogs for every repository. The
[design reference](designs/test-planning.md#policy-v1) specifies all fields, limits, and selection rules.

## Plan an explicit source

```bash
yaga test plan --policy .yaga/test-policy.toml --profile development --range BASE...CANDIDATE
yaga test plan --policy .yaga/test-policy.toml --profile development --range BASE..CANDIDATE --format json
yaga test plan --policy .yaga/test-policy.toml --profile integration --revision CANDIDATE --format json
```

Replace `BASE` and `CANDIDATE` with explicit commit IDs or commit-ish arguments available locally.
`--policy`, `--profile`, and exactly one source option are required. `--repo` defaults to `.`;
it locates the repository but does not change how a supplied policy path is resolved. The command
never assumes `HEAD`, selects the index or working tree, reads an event, discovers a branch, or
fetches missing objects. Dirty and untracked files do not enter the comparison.

| Source | Comparison and execution identity | History requirement |
| --- | --- | --- |
| `--range A..B` | Compare A's tree to B's tree; execute B | Complete, non-shallow history |
| `--range A...B` | Compare the unique merge base of A/B to B; execute B | Complete, non-shallow history and one merge base |
| `--revision B` | Execute B with no comparison; full profile only | Selected commit/tree must exist; shallow history is allowed |

The report binds resolved exact object IDs, including the execution commit's tree. Revision aliases
and local directory names do not enter the digest. All operations use the shared bounded Git runtime
with no lazy fetching, replacement refs, or legacy graft overlays. Ranges disable rename detection,
external diff, and text conversion; renames therefore expose both old and new paths. Deletions, mode
changes, and gitlink changes remain visible. Submodules are not traversed.

A full profile may also use a range. It validates and records the complete comparison but selects
the full catalog without classifying unknown paths. A revision-only source cannot use a selective
profile, because it has no changed-path set.

**Run the planned execution commit.** A PR-head plan does not qualify a different merge candidate.
Planning against committed objects also does not establish that a later dirty-worktree execution
used those objects.

## Understand the selection

Rules add obligations. Each matching rule selects its named suites and every suite carrying one of
its selected tags. The profile's `always` suites remain required. Prerequisite suites are added
transitively and ordered before dependents; every selected suite retains all of its variants.

A `full-on` match selects the entire catalog. Otherwise an unknown path defaults to full selection,
or causes an error when `unknown-paths = "error"`. `no-impact` acknowledges a path without adding a
suite, but cannot undo another rule or a mandatory obligation. An empty diff selects only `always`;
an empty selection is a valid zero-obligation plan, not a test success.

| Reason code | Meaning |
| --- | --- |
| `test.always` | The selected profile requires this suite |
| `test.rule` | A path matched a rule that selects this suite |
| `test.profile-full` | The requested profile requires the whole catalog |
| `test.full-trigger` | A path matched a full-selection trigger |
| `test.unknown-path` | An unmatched path triggered full fallback |
| `test.no-impact` | A path was explicitly acknowledged without an additional obligation |
| `test.prerequisite` | A selected suite requires this prerequisite |

Human output sanitizes untrusted text and displays at most 256 reasons, with an exact omitted count.
JSON retains the complete inventory, selection, and reasons. An oversized report fails instead of
truncating obligations or producing a partial plan. Policy is limited to 128 KiB; plans to 4 MiB;
the catalog to 128 suites and 512 total variant cells; changed paths to 2,048 entries and 4 MiB.
See [all limits](designs/test-planning.md#policy-v1).

## JSON contracts and exit codes

Both commands accept `--format text|json` and `--quiet`. Text is the default. Quiet suppresses
successful output in either format while retaining errors. Successful output goes to stdout. Errors
go to stderr. With JSON selected, policy, input, and Git errors handled by the command use YAGA's
existing bounded `schema_version = 1` error envelope. CLI usage errors, such as a missing required
option or an unsupported format, retain the standard CLI error display.

Policy-check JSON has these fields:

```json
{
  "schema_version": 1,
  "kind": "test_policy",
  "status": "valid",
  "valid": true,
  "test-policy-version": 1,
  "policy_sha256": "<SHA-256 of exact policy bytes>",
  "suites": 2,
  "cells": 3,
  "profiles": ["development", "integration"],
  "rules": 2
}
```

A plan report has exactly three envelope fields: `test-plan-version` (integer `1`), `plan_digest`,
and `plan`. The semantic `plan` object contains:

| Field | Value |
| --- | --- |
| `test-plan-version` | Integer `1`, included in the digest |
| `policy_sha256` | SHA-256 of the exact policy bytes, including comments |
| `bindings_sha256` | Supplied binding-manifest SHA-256 or null |
| `profile`, `requested_mode`, `effective_mode` | Selected profile and mode before/after fallback |
| `source` | `base_commit`, `head_commit`, `comparison_commit`, `execution_commit`, `execution_tree` |
| `changed_paths` | Complete sorted paths, or null for a revision-only plan |
| `inventory` | Complete normalized suite definitions, including unselected suites |
| `selected` | Topologically ordered `{suite, variants}` records with sorted variant IDs |
| `reasons` | Sorted, deduplicated `{code, suite, rule, path, required_by}` records |

For revision-only plans, `base_commit`, `head_commit`, and `comparison_commit` are null. Every reason
has all five keys; irrelevant values are null. Inventory is sorted by suite ID and contains the
suite's `id`, `binding`, `tags`, `variants`, `needs`, and `requires`. Selection order uses
lexicographic tie-breaking among available prerequisites. Reasons sort by the five listed fields,
with null before strings.

`plan_digest` is SHA-256 of the `plan` object encoded as UTF-8 JSON with sorted keys, compact
separators, and no ASCII escaping. Complete canonical arrays participate; timestamps, local paths,
and human formatting do not. Identical policy bytes, source identities, binding identity, and profile
produce an identical digest. A comment edit changes the policy hash and therefore the digest.
The CLI emits plans; it does not import plan documents or evaluate execution results. The
[executor handoff API](test-planning-handoff.md) accepts freshly computed validated plans.

| Exit | Meaning |
| --- | --- |
| `0` | Valid policy or complete plan, including full fallback and zero-obligation selection |
| `2` | Invalid policy/input, missing or incomplete history, invalid Git output, or resource failure |

Neither command uses exit `1` or reports that tests passed. A collection error produces no plan.
CI must block or use a separately defined full path when planning fails; missing output must never
be interpreted as an empty selection.

## Use plans in CI

Choose the profile explicitly from reviewed CI policy. Protected consolidation/release candidates
normally use a full profile. Load the expected catalog and binding registry from a trusted revision;
full selection of a PR-edited catalog cannot detect obligations that the contributor removed.
Policy and binding digests identify bytes, not authority, signatures, or successful execution.

For executor handoff, pass `--bindings-sha256` with the lowercase 64-character SHA-256 of an
immutable reviewed execution manifest. The Python `ExecutorRegistry` provides a canonical identity
for a complete fixed job map and its definition hashes. Its definitions should cover the registry's
revision and locked tools/images/configuration for every variant. The CLI treats the digest as
opaque; the handoff API checks it against the supplied registry. Without it, `bindings_sha256` is
null and the plan is a preview that cannot be handed off.

Keep existing required CI running during initial comparison. Review the selected suites and
variants against those jobs, then follow the [shadow pilot and activation plan](designs/test-planning.md#shadow-pilot-and-promotion-criteria).
The [handoff and shadow guide](test-planning-handoff.md) includes a runnable adapter for both
catalogs, fixed maps for all five execution categories, and the django-ray pilot protocol.
Conditional jobs alone are insufficient: an eventual required aggregate must detect failed,
cancelled, missing, or unexpectedly skipped selected cells and failed cleanup. This release supplies
planning and fixed-job handoff; generic receipt evaluation and selective-execution activation remain
later phases. The pilot keeps existing required CI authoritative. It does not provide a
merge-security authority or authorize credentials or write access.
