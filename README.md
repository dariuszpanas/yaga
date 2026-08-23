# YAGA

YAGA is Yet Another Gate Action: a reusable, conservative GitHub status gate. Its first adapter
turns Codex GitHub review evidence into two authoritative classic commit statuses, `Codex Review`
and final `CI Gate`. Generic GitHub transport and publication remain separate from Codex policy so
later provider adapters can reuse the trust boundary.

YAGA is pre-release. Pin the exact audited 40-character commit SHA that your repository canaried;
do not consume a branch or mutable tag.

## Why the workflow is split

Write-capable review automation must not execute pull-request-controlled code. YAGA therefore puts
two trusted, write-capable default-branch workflows around ordinary unprivileged PR CI:

1. [`examples/review-policy.yml`](examples/review-policy.yml) is a tiny `pull_request_target`
   invalidator. A real lifecycle boundary uses the native job/check name `Review Policy Boundary`;
   an ordinary title/body edit instead uses `Review Policy Metadata`, which is not a required
   context. YAGA verifies the live PR, unique head ownership, and commit-status capacity before
   writing `Codex Review=pending` and `CI Gate=pending`. It checks out no code, requests no review,
   and does not trigger on `closed`.
2. Normal unprivileged PR CI runs the repository's tests and ends at a check named
   `CI Prerequisites`.
3. [`examples/codex-review.yml`](examples/codex-review.yml) wakes on a completed `CI` or
   `YAGA Review Policy` run. It re-fetches and authenticates the wake, resolves the exact current CI
   and lifecycle pair, requires a unique ready PR, and never reads artifacts or cache. Failed CI
   publishes terminal errors without asking Codex. Successful CI routes one bounded request or
   observes only a review already started by the exact current-boundary YAGA request. If CI
   completes first, `prepare` waits for the lifecycle run for at most two minutes; a later lifecycle
   completion also provides a second reconcile wake.
   Before any status write, YAGA deterministically elects only the later completion wake (CI wins an
   exact timestamp tie), so either queue order makes progress without two processors churning the
   same boundary.
4. A final independent operation revalidates CI, the live lifecycle boundary, authorization, and
   exact-head Codex evidence before publishing classic `CI Gate=success` last.

The configured display names `CI` and `YAGA Review Policy` are coupled to the publisher's
`workflows` trigger, while their authenticated file paths are coupled to `prerequisite-workflow` and
`lifecycle-workflow`. Keep the names and paths synchronized. The CI workflow must trigger on
`opened`, `synchronize`, `reopened`, and
`ready_for_review`, and use the exact human run-name
`YAGA CI <action> for #<pull-request> at base <full-base-SHA>`. YAGA authenticates those bounded
action, PR, and base fields before accepting a completed `workflow_run`; contributor text must not
appear in that title.

The CI result is deliberately a quota-saving prerequisite and heuristic under YAGA's beta trust
model, not a security authority: PR-controlled code can alter its own CI behavior. The protected
environment and owner identity protect YAGA's quota path; `Maintainer Approval`, required review,
and audits of write-capable workflows and integrations remain part of merge security.

All workflows have friendly human run titles. The names `Codex Review` and `CI Gate` are reserved
for classic commit statuses and must not be used as workflow, job, or check names.

## Quota firewall

Create the required repository Actions variable `YAGA_CODEX_OWNER_ID` with the immutable numeric
GitHub user ID allowed to request Codex directly. The generic template contains no account-specific
ID. Every other PR author is routed through a precreated `codex-review-approval` environment.
Configure that environment with the quota owner as its sole required reviewer, enable prevent
self-review, disable administrator bypass, and store no secrets. Add the environment variable
`YAGA_CODEX_APPROVAL_MARKER=codex-review-approval:v1`. The template uses `deployment: false`, so the
protection applies without creating a deployment record.

GitHub required-reviewer environment protection is available for public repositories on supported
plans and for private or internal repositories on GitHub Enterprise, subject to GitHub's current
plan rules. Confirm the feature and the exact protection settings in a preflight, then prove the
wait and approval behavior on a canary before enabling YAGA for contributors.

The protected external flow is intentionally split. The per-PR `authorize-external` job cancels a
stale approval wait and, after environment approval, records only a non-triggering approval marker.
A separate per-PR worker never cancels an in-flight run; it re-reads that marker and posts a review
request only when one is still needed. Owner requests use the same non-cancelling worker directly.
Within that trusted mutex, YAGA posts at most one strictly marked quota-consuming request for a
lifecycle boundary. The marker binds the repository, PR, full head and base SHAs, lifecycle run,
and authorizing CI run/attempt. YAGA lists the complete bounded comment history before and after its
single POST and never blindly retries an ambiguous write. CI reruns reuse the existing boundary
request. Immediately before that POST, YAGA revalidates the candidate and approval and re-reads the
exact request plus admissible pending/outcome evidence. GitHub cannot make the last read and comment
POST atomic, so another actor can still start a review in that final interval; disabling automatic
reviews removes the normal provider-side source of that residual race.

Disable Codex automatic reviews and drain every existing Codex task before enabling YAGA's v2
publisher. YAGA is then the sole legitimate automatic requester and asks only after CI passes.
Canary both the owner request and protected-external approval plus request paths. Every accepted
eyes reaction or outcome, including on `opened`, must follow the exact current-boundary
Actions-owned YAGA request marker. Visible unsolicited connector activity fails closed without
posting a duplicate YAGA request. External approval never reuses unsolicited evidence: it
authorizes YAGA to create the exact request and nothing else.

The environment protects only YAGA's token. OpenAI's public GitHub documentation does not describe
a repository control that prevents a person or another app from directly posting `@codex review`.
Such a comment may still consume provider quota. If its activity is already visible, YAGA fails
closed rather than spending quota on a second request. A direct request can still race YAGA's final
read and comment POST, however, and Codex outcomes do not identify the triggering comment. The
current request marker therefore supplies temporal correlation, not a native provider binding.
Only that protected route's exact YAGA marker authorizes an external-author request. Repository
permissions and provider-side access controls remain necessary.

## Action interface

| Input | Contract |
| --- | --- |
| `gate` | Closed selector; currently only `codex-review`. |
| `operation` | One of `invalidate`, `prepare`, `authorize`, `observe`, `request`, or `finalize`. |
| `github-token` | Token passed only through the environment. |
| `prerequisite-workflow` | Authenticated PR CI path; default `.github/workflows/ci.yml`. |
| `lifecycle-workflow` | Trusted invalidator path; default `.github/workflows/review-policy.yml`. |
| `owner-id` | Immutable direct-request author ID; other authors use environment approval. |
| `approval-marker` | Protected-environment capability used only by `authorize`; otherwise empty. |
| `request-timeout` | Bounded REST timeout; default `5` seconds. |
| `job-timeout-minutes` | Bounded action window; the templates use `15`. |

`prepare` exposes the closed `route` output (`skip`, `done`, `observe`, `owner`, `external`, or
`approved`) and an exact `pull_request_number` output used only for job concurrency. Candidate
identity is never passed through workflow outputs; every operation re-reads trusted API state after
any queue or environment wait.

## Evidence contract

YAGA accepts only exact Codex connector identities and these bounded outcomes:

- a clean connector issue comment whose reviewed-commit marker resolves to the current head;
- a formal connector findings review whose native `commit_id` and reviewed-commit marker identify
  the current head; or
- the connector's `+1` reaction.

Every outcome must be strictly later than the exact current-boundary Actions-owned YAGA request
marker, including on the initial non-draft `opened` boundary. An eyes reaction is progress, not
success, and selects the non-commenting `observe` path only when it is likewise bound to that exact
request. Visible connector activity without the marker is an invariant violation: YAGA fails closed
without posting a duplicate request, and external approval never converts or reuses that unsolicited
evidence.

A PR-body reaction has no commit/base identifier of its own, and even a reviewed-commit marker does
not identify the triggering request. A delayed review of an older head can finish after a newer
boundary, so the YAGA marker establishes temporal correlation rather than a native provider
binding. Automatic reviews must be disabled and drained before activation, and no direct or other
integration-triggered Codex review can overlap YAGA. YAGA also requires trusted successful status
lineage for every older YAGA request before it can post or trust evidence for a newer boundary. If a
head changes while its request is unresolved or timed out, open a fresh PR. A deployment that cannot
prevent overlapping non-YAGA reviews must not enable this beta action.

A formal findings review completes YAGA's evidence check. Consumers must also enable GitHub required
conversation resolution so unresolved findings remain merge-blocking. Findings outside a resolvable
review thread require a later clean review if the repository wants them to block.

## Lifecycle and recovery

The invalidator handles `opened`, `synchronize`, `reopened`, `edited`, `ready_for_review`, and
`converted_to_draft` on the default branch. Ordinary title/body edits are local no-ops, receive the
native `Review Policy Metadata` name, and use a unique concurrency group so they cannot cancel or
replace the required `Review Policy Boundary` check. Base-changing edits revoke success; push a new
commit to obtain fresh CI. Draft transitions remain pending until ready.

The publisher runs only after a completed CI or lifecycle workflow. Its entry job accepts the exact
`.github/workflows/ci.yml` plus `pull_request` pair or the exact
`.github/workflows/review-policy.yml` plus `pull_request_target` pair. The lifecycle completion may
therefore run against the `main` base branch, while post-merge CI has event `push` and skips every
publisher job before YAGA runs. Fork head branches named `main` remain eligible. There are no
comment, review, schedule, merge-queue, or `closed` wakes.
A delayed invalidator re-reads the exact live PR before writing and therefore skips a PR that is
already closed. Closure cannot start a new publisher run, but it can race an already-running
worker's final live read and subsequent comment or status POST because GitHub REST provides no
transaction spanning those operations. A marked request or status can therefore land after close
and a request may consume quota; YAGA revalidates and compensates where possible, but does not
guarantee zero post-close writes.

Polling is bounded. A timeout publishes `Codex Review=error`; rerun CI after Codex or GitHub
recovers. There is no scheduled repair. Polling admits another evidence pass only while a fixed
terminal-error request/time tail remains. Commit-status writes are not transactional, so runner loss
can leave pending. A newer CI/lifecycle run supersedes older attempts, and every request or
terminal write revalidates the live open PR as closely as GitHub REST permits. YAGA also reserves
status slots before each lifecycle boundary. Comments, reviews, and reactions each use one complete
page; 100 or more records is treated as incomplete and fails closed. More than eight older YAGA
request boundaries also fail closed, so continue on a new PR. Commit-status reads likewise require
a complete page with fewer than 100 visible statuses across
all contexts, and terminal publication reserves the last two visible slots for its write and one
repair. Push a new commit before that page fills or before the longer per-context status ceiling is
approached; either condition otherwise leaves the gate pending and requires a new head SHA.

## Required repository policy

Require strict, up-to-date `Commit Messages`, `Maintainer Approval`, `Review Policy Boundary`,
`CI Prerequisites`, `Codex Review`, and `CI Gate` as applicable. The native lifecycle check makes a
runner/API failure block before a new boundary can inherit old classic green; the native CI check
also blocks failed or in-progress reruns. Keep required conversation resolution enabled. Only YAGA
publishes classic `CI Gate` after review succeeds.
Merge queues are unsupported because YAGA has no `merge_group` trigger or combined-head contract.

The beta writer is the shared GitHub Actions integration, not a dedicated YAGA GitHub App. Audit
every default-branch workflow and integration with `statuses: write` or `pull-requests: write`,
reserve every case-insensitive `Codex Review` alias and every `CI Gate` alias, and keep repository
Actions defaults read-only. The three comment-writing jobs use `pull-requests: write`; live GitHub
Actions evidence showed that `issues: write` alone received HTTP 403 for the PR conversation route.
A same-named Actions check cannot replace a classic status: when GitHub requires both, both must
pass. A collision can still create ambiguity or denial of service. A dedicated App selected as the
expected status source is future hardening.

This beta also assumes GitHub delivers every configured lifecycle event. If a reopen or other
same-head transition is not delivered, GitHub does not create the new native boundary check and an
older green result can remain visible. There is deliberately no scheduled repair wake; verify event
delivery in the canary and treat missing delivery as a deployment blocker.

Before rollout:

1. Remove every legacy observer, schedule, comment wake, and duplicate status writer.
2. Precreate and verify the protected environment and the two required variables described above.
3. Rename the ordinary CI terminal check to `CI Prerequisites`; add the exact prerequisite trigger
   set and `YAGA CI <action> for #<pull-request> at base <full-base-SHA>` run-name. Keep the source
   workflow display names `CI` and `YAGA Review Policy` synchronized with the publisher's
   `workflows` trigger, keep their file paths synchronized with `prerequisite-workflow` and
   `lifecycle-workflow`, and install both templates at their configured paths. For a default branch
   other than `main`, replace `branches: [main]` in the lifecycle workflow.
4. Freeze new PRs, reach zero open PRs, disable automatic reviews, and drain every existing Codex
   task before enabling the publisher. Pin the audited YAGA SHA, then open fresh canaries for failed
   CI, the owner request, protected-external approval plus request, timeout/rerun, metadata and
   lifecycle transitions, close, and merge. Confirm the post-merge publisher run skips every job.
5. Require `Review Policy Boundary`, `CI Prerequisites`, `Codex Review`, and `CI Gate` only after the
   exact canary heads succeed and before normal contributor traffic.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development details and [SECURITY.md](SECURITY.md) for
private vulnerability reporting and the beta trust boundary.
