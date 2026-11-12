# Pull-request trust triage

`yaga pull-request triage check` separates repository admission from automated code-review
results. An external author's same-repository or fork PR stays on hold until a configured
maintainer submits a current-head `APPROVED` review containing an explicit boundary acknowledgment.
Deterministic sensitive-path and exact
merged-tree findings help a maintainer inspect the change; they do not establish malicious intent.
Account age, activity, fork count, labels, and comment instructions are not admission evidence.

## Explicit trusted policy

Create a starter with `yaga policy init triage` or copy
[`examples/triage-policy.toml`](https://github.com/dariuszpanas/yaga/blob/main/examples/triage-policy.toml).
Configure the repository and maintainer's immutable numeric IDs from authenticated GitHub metadata.
The generated empty maintainer list admits nobody. Names and `author_association` are insufficient.

```toml
triage-policy-version = 1
host = "github.com"
repository-id = 123456       # replace with your verified repository ID
maintainer-user-ids = [1234] # replace with your verified maintainer User IDs
duplicate-candidates = 5
```

Only explicitly configured User IDs can acknowledge external changes. The latest substantive
review from each configured maintainer is considered. An approval must reference the current
head, contain the exact acknowledgment from the current report, and cannot be the author's own
review. Dismissal or a later `CHANGES_REQUESTED` invalidates
that maintainer's approval; any configured maintainer's latest change request holds admission.
A new head, base SHA, base branch, or trusted policy needs a fresh acknowledgment. Comment-only
reviews do not supersede substantive decisions. The acknowledgment is an exact standalone line:
`<!-- yaga-pr-triage-acceptance:v1 DIGEST -->`, where `DIGEST` is SHA-256 of canonical JSON binding
host, repository ID, PR number, head SHA, base SHA, base ref, and the trusted policy's raw-byte
SHA-256 digest. YAGA supplies the complete line in the report and comment. An ordinary approval
without it stays on hold because GitHub's native review record binds only the head. Multiple
acceptance markers are ambiguous and cannot grant admission. This new marker is separate from
the Agent review gate's quota authorization markers. Only the configured User's formal review
can acknowledge the boundary; a bot publishing the template does not grant permission.

A configured maintainer's own same-repository PR is admitted automatically. An optional automation
entry requires its exact immutable Bot ID, exact login, same repository ID, every changed path
within `allowed-patterns`, no rename/copy, and no sensitive-path or duplicate-tree finding:

```toml
[[trusted-bots]]
user-id = 49699333 # example only: independently verify for your GitHub host
login = "dependabot[bot]"
allowed-patterns = ["uv.lock", "pyproject.toml"]
```

This classifies the PR author and source repository, not every commit's writer or dependency's
safety. Keep repository write access and automation path allowances narrow. Forked maintainer or
bot PRs need an independent configured maintainer's current-head review.

Default sensitive patterns cover `.github/**`, `.yaga/**`, `.yaga.toml`, and root or nested
`AGENTS.md`, `CODEOWNERS`, `SECURITY.md`, and `CONTRIBUTING.md`. An explicit `sensitive-patterns`
list replaces these defaults. Both sides of renames are inspected. Patterns use YAGA's bounded
repository-relative path grammar. Findings do not veto an explicit maintainer acknowledgment.

## Commands and reports

```bash
yaga pull-request triage check --policy .yaga/triage-policy.toml \
  --pr 123 --repo OWNER/REPO --format json
yaga pull-request triage check --policy .yaga/triage-policy.toml \
  --pr https://github.com/OWNER/REPO/pull/123 --publish-comment --format json
yaga pull-request triage check --policy .yaga/triage-policy.toml \
  --file triage-snapshot.json --format json
```

Exit codes are `0` for allowed admission, `1` for a hold, and `2` for configuration, inventory,
or operational errors. Unknown schema fields, missing IDs, deleted head repositories, malformed
review state, truncated inventories, and API budget exhaustion cannot yield admission. JSON
schema version 1 includes the exact boundary, policy digest, classification, inventory scope,
admission reason/review ID, exact finding count, and up to 256 findings. Text is terminal-sanitized.

The duplicate scan compares the entire head Git tree with merge-commit trees from up to the
configured number of most recently updated closed PRs (excluding the target PR itself). Unmerged
closed PRs consume candidate slots. A match is exact; a nonmatch says nothing about older PRs,
equivalent patches, partial copies, or rebases onto a different base. Set
`duplicate-candidates = 0` to explicitly disable this finding. File contents, PR descriptions,
diffs, and instruction text are never interpreted as commands or policy. This check does not
perform semantic prompt-injection analysis.

Offline input is an explicit metadata snapshot, not a live authorization receipt. It has
`version = 1`; host/repository/PR IDs and names; `state`, `draft`, head/base SHA and base ref;
head tree and repository ID/fork flag; author ID/login/type; complete files and review lists;
and the bounded merged-candidate inventory. File records contain `filename`, `previous_filename`,
`sha`, `status`; review records contain `id`, `user_id`, `user_type`, `state`, `commit_id`,
`submitted_at`, `body`; candidate records contain `number`, `merge_commit_sha`, `tree_sha`.
Top-level `changed_files`, `candidate_limit`, and `candidates_examined` must agree with the
inventory and policy. Offline input cannot publish comments or formal reviews and must not be
used as authority by a privileged workflow. The JSON report is not an input snapshot.

## Publication and enforcement

Live fetches use installed authenticated `gh`, without a shell. `--publish-comment` is explicit;
it creates or updates one marker-owned comment for the authenticated account. Repeated identical
reports make no write. A head or admission change updates the same comment. A duplicate, foreign,
or malformed marker fails closed. Installation tokens unable to query `/user` must configure
`comment-author-id` with the verified publisher's immutable ID; the API response must match.
Use one trusted writer/concurrency group per PR to avoid simultaneous create races.

The comment contains fixed summaries, IDs, a canonical report digest, the acknowledgment template,
and up to 10 findings with escaped paths (200 characters each). Control/format characters are made
visible, and Markdown/HTML in paths is inert. The bounded JSON retains complete escaped paths.
The REST file endpoint may return bounded raw patches; YAGA discards those fields without
interpreting them. The client
rechecks live state before a write; GitHub does not offer an atomic compare-and-swap between these
reads and comment/review publication. Consumers must bind decisions to the current head and use
GitHub's stale-review protections. A comment is informative and never authorizes a merge.

Automatic positive formal reviews now require `--triage-policy`:

```bash
yaga pull-request review check --policy .yaga/agent-review-policy.toml \
  --triage-policy .yaga/triage-policy.toml --pr 123 --repo OWNER/REPO --submit-review both
```

The root Action exposes the same path as `triage-policy` for `check: pr-review`. Run the separate
`check: pr-triage` step first to publish the useful comment automatically:

```yaml
- name: Triage admission and publish review surfaces
  uses: dariuszpanas/yaga@0000000000000000000000000000000000000000 # replace with audited SHA
  with:
    check: pr-triage
    policy: .yaga/triage-policy.toml
    pr: ${{ github.event.pull_request.number }}
    repo: ${{ github.repository }}
    publish-comment: "true"
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

This step needs `issues: write` and trusted default-branch policy including the verified
`comment-author-id` for installation tokens. `publish-comment` accepts only lowercase `true`
or `false` (omission is false). Offline `file` mode rejects credentials and publication. Keep
normal failure propagation: exit `1` on hold prevents subsequent formal approval. Do not use
`continue-on-error` to turn admission holds into successful publisher jobs. Existing
read-only review checks and negative-only publication remain available without this policy.
Code-review success alone cannot admit an external PR. Its maintainer approval must also contain
the exact current boundary acknowledgment. Before positive publication, YAGA finishes its bounded
file and tree inventory, then collects current code-review evidence and admission state together.
Both decisions use the same final formal-review payload, including actual User/Bot identities;
current comments and configured check runs are collected after tree work. A final metadata read
rejects boundary changes before submission with an explicit `commit_id`. These sequential API
reads and the final write are not atomic. Approval by Codex or another bot, a wake comment,
a removable label, and the separate Agent review gate's quota authorization marker cannot
substitute for the configured User review.

Withholding a new approval does not revoke an older bot approval on the same head after a base,
policy, or maintainer-review change. This feature does not publish an authoritative exact-head
status or alter merge rules. A trusted `pull_request_target` or `workflow_run` job can be associated
with the base rather than the PR head; requiring its name alone is not a reliable current-head
admission gate. Live merge enforcement needs a separate trusted, current-head status integration
and repository rules alongside existing CI/review requirements. Do not treat a skipped, missing,
or historical triage result as permission to merge.

YAGA's dogfood `Publish Pull Request Review` job posts triage before formal review. Its trusted
publisher reacts to opened/synchronized/reopened/ready/edited PRs; the tokenless wake workflow
reacts to submitted/edited/dismissed User or Bot reviews, selected bot comments, and manual
dispatch. Wake events request fresh evaluation and never authorize admission. The minimal
example covers initial PR events; add the trusted tokenless wake plus default-branch
`workflow_run` pattern or rerun the trusted publisher after a maintainer acknowledges the boundary.
Base-branch pushes and policy edits do not themselves reevaluate every open PR. A future merge
enforcement integration must cover those changes and validate current facts before merging.

This command never changes repository rules, approves fork workflow execution, dismisses existing
reviews, or merges a PR. A repository needing owner-only merge must also configure GitHub's merge
restrictions. An API call made by an assistant using a maintainer's
credentials is indistinguishable from that maintainer: require explicit per-PR and per-head user
authorization in the repository's agent instructions before an assistant creates such a review.

Use trusted default-branch code and policy in privileged metadata jobs. Never checkout, install,
import, or execute fork/PR code, or load PR-provided artifacts/cache as authorization. Give reads
only read permissions; add `issues: write` for comments or `pull-requests: write` for reviews.
Run untrusted tests separately without those credentials. The triage package remains outside the
stdlib-only `check: gate` and tokenless commit Action import graphs.

## Bounds

| Resource | Limit |
| --- | --- |
| Policy | 64 KiB, schema v1, at most 32 maintainer IDs and 32 bots |
| Path patterns | 1–64 per list, cumulative matching work capped |
| Changed files | 500 complete records; filename at most 4096 UTF-8 bytes/64 segments |
| Reviews/comments | At most 99 each; a full 100-record page fails closed |
| Review body | 64 KiB; only the exact acceptance marker is interpreted |
| Duplicate candidates | 0–20 closed PRs, default 5 |
| API operations | 64 requests, 300 seconds total admission budget |
| Each `gh` process | 30 seconds, 512 KiB stdout, 64 KiB stderr |
| Offline snapshot / JSON report | 1 MiB each; 256 retained findings |
| Published comment | 16 KiB; existing comment body at most 64 KiB |

These are deliberate fail-closed operating limits. Large or ambiguous PRs require a separate
maintainer-controlled review route; increasing a limit needs corresponding resource validation.
