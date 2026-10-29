# Staged repository checks: design assessment

## Decision for the next release

Complete the assessment requested in [#243](https://github.com/dariuszpanas/yaga/issues/243);
retain the existing committed-only commands in this release. An explicit index source is useful
for local feedback before a commit, especially for required paths, forbidden generated files,
executable bits, portability, and oversized blobs. It needs a separate source and reporting
contract before implementation. This assessment does not introduce a `--staged` option.

The current result is intentional: creating or staging a required file does not change
`tree check --revision HEAD`; committing it does. Workflows and literal message/description
checks already offer useful checks before committing. Staged checks would complement them and
would not establish the final commit identity or replace committed CI checks.

## Proposed scope

Start with an explicit `--staged` source for `tree`, `path`, `mode`, and `size`, mutually exclusive
with `--revision`. Require exactly one source and an explicit policy, retaining `--repo .` as the
only repository default. Do not infer HEAD, merge staged and working files, include untracked
files, discover policy, fetch objects, or write the caller's index. Repository-plan integration,
change coupling, test planning, and both composite Actions remain separate decisions.

Reuse each provider's pure checker and policy version where its meaning stays identical. The
adapter should provide the complete proposed index tree, not just changed entries, so required
paths and total-size policy have their normal meaning. Reuse the shared bounded Git runtime;
do not create a commit or use `write-tree` to manufacture a committed identity.

## Required semantics

| Index state | Proposed behavior |
| --- | --- |
| Addition or deletion | Check the complete index after the staged change; absence is a deletion. |
| Rename | Treat the indexed destination and removed source as paths; no rename detection. |
| Partial staging | Inspect indexed blob identities and sizes, never current file contents. |
| Unstaged or untracked work | Exclude it, including edits made after `git add`. |
| Unmerged entries | Return an operational error; never choose a conflict stage. |
| Intent-to-add entry | Return an operational error until actual content is staged; do not silently treat it as an empty blob. |
| Symlink | Check the indexed mode and stored link-blob size; never follow/read its target. |
| Executable mode | Check the index mode, independent of host permissions. |
| Gitlink | Treat it as one leaf, never recurse; size excludes it as today. |
| LFS pointer | Count the stored pointer bytes, not downloaded LFS content. |
| Duplicate blob ID | Count bytes per path, as in the committed size provider. |
| Missing required object | Return an operational error without lazy fetch. |
| Sparse or split index | Support only after proving complete expansion and snapshot identity; initial implementation may reject these explicitly. |
| Index changes during collection | Fail the collection or operate on a validated private snapshot; never mix observations. |

An unborn repository can have a valid staged snapshot without a HEAD commit. Empty index behavior
must be explicit: tree requirements can fail, while empty path/mode/size input can pass according
to policy. The snapshot covers the entire repository even when `--repo` names a subdirectory.

## Snapshot and trust requirements

Before implementation, a small native-Git spike must prove a bounded way to capture an index
snapshot and identify unmerged, intent-to-add, sparse, split, and unsupported extension states.
Plain `ls-files --stage` output alone is insufficient evidence for all of these distinctions.
Private snapshotting must account for linked worktrees and shared-index dependencies and validate
regular-file type, identity, size, and any before/after race observations. If a stable snapshot
cannot be established, fail closed. Document the concurrency guarantee instead of presenting
two matching reads as an atomic transaction against a malicious writer.

Use shell-free fixed Git commands with the existing trusted executable, isolated environment,
no prompts, no replacements, no lazy fetch, bounded cleanup, and graft checks. An implementation
must not run hooks, filters, external diff/textconv, or inspect symlink targets. Size lookup should
read object metadata by validated object ID, not materialize file contents. Keep the existing
30-second process, 64 MiB tree-output, 50,000-entry, 4,096-byte path, 64-component path,
10,000,000 matcher-work-unit, portable-integer, and 256-stored-diagnostic boundaries.

## Reporting compatibility decision

Keep committed-source schema-v1 reports unchanged. Introduce a separately versioned staged
report envelope before exposing the new source; do not put an index hash in `commit_sha` or
`tree_sha`, or emit invented/null identities under an unchanged committed schema. The proposed
envelope needs an explicit `source.kind = index`, a canonical ordered entry-manifest digest,
and policy/source identity sufficient to distinguish repeated observations. Specify the digest
grammar and object hash algorithm before use. It identifies observed input, not a trusted origin
or a commit that will later be created.

Preserve provider diagnostic codes, exact totals, first-pattern attribution, text/JSON/GitHub
formats, and exits 0/1/2. Text and annotations must say staged/index. Report schema selection
and future repository aggregation need explicit compatibility tests and documentation.

## Implementation gate

Implementation is appropriate after a concrete local-hook/editor consumer confirms the need and
the snapshot spike passes on Linux and Windows with supported Git versions. Acceptance must cover
every row above, linked worktrees, subdirectory selection, hostile Git environment/configuration,
malformed/bounded output, missing objects, race failures, and clean temporary-file ownership.
Installed-wheel tests must compare staged versus committed results and verify that neither index
bytes nor repository history change. Required CI continues checking exact committed sources.

This closes the design assessment for the next release. Shipping staged execution requires a
separate implementation decision and the above evidence; it is not part of this release's CLI.
