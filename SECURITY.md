# Security policy

YAGA is security-sensitive GitHub Actions infrastructure. Do not open a public issue for a
suspected vulnerability. Use GitHub private vulnerability reporting and include the affected
commit, trust boundary, expected fail-closed behavior, and a secret-free reproduction.

Useful reports include:

- PR-controlled code, actions, artifacts, caches, or text reaching a write-capable trusted job;
- a request comment posted before exact current successful CI, live PR/head/base/default branch,
  unique ownership, lifecycle provenance, and owner/environment authorization are revalidated;
- `Codex Review=success` or `CI Gate=success` without exact connector evidence and current status
  lineage;
- an external-author outcome passing without the protected YAGA authorization marker;
- inherited success reused after synchronize, reopen, draft/ready, or base change;
- a close, cancellation, rerun, or race producing authoritative stale success, or a post-close
  request outside the documented final-read/comment-POST residual;
- unbounded API, polling, body, pagination, status-history, or comment-history behavior; or
- another writer or case-insensitive alias replacing either reserved status context.

The lifecycle invalidator and `workflow_run` publisher execute only trusted default-branch action
code. They never check out PR code, consume upstream artifacts/cache, or interpolate untrusted text
into shell. The publisher authenticates completed `CI` and `YAGA Review Policy` wakes and re-fetches
the exact current CI/lifecycle pair and candidate after queue or environment waits. A CI-first wake
waits at most two minutes for lifecycle publication; a later lifecycle completion provides a second
reconcile wake. Before either path writes, the later completion is elected as the sole processor;
CI wins an exact timestamp tie so a guaranteed source wake retains liveness.

PR CI is untrusted and is only a quota-saving prerequisite/heuristic under the beta threat model;
it is not a security authority and does not by itself prove that contributor-controlled tests were
honest. The immutable owner ID and protected environment secure YAGA's quota path. Maintainer
approval, required review, and an audit of all write-capable workflows and integrations remain part
of merge security.

Consumers must set the required repository variable `YAGA_CODEX_OWNER_ID`. The external route uses
the literal precreated `codex-review-approval` environment with `deployment: false`; configure the
quota owner as its sole required reviewer, prevent self-review, disable administrator bypass, store
no environment secrets, and define the environment variable
`YAGA_CODEX_APPROVAL_MARKER=codex-review-approval:v1`. Required-reviewer environments are available
for public repositories on supported GitHub plans and for private/internal repositories on GitHub
Enterprise, subject to GitHub's current rules. Verify that support and protection in a preflight and
canary before relying on it. Disable Codex automatic reviews only after that canary proves YAGA's
request path, then repeat owner and protected-external request canaries.

The cancel-stale per-PR `authorize-external` job records only a non-triggering approval marker. A
separate PR-serialized request worker does not cancel an in-flight run and may post `@codex review`
only after revalidating that marker. This split keeps a stale approval wait from blocking a newer
boundary without allowing cancellation to duplicate a request.

The environment protects only YAGA's request token. It cannot stop a person or other integration
from directly posting `@codex review`; provider-side execution may still consume quota. YAGA does
not count that unsolicited outcome as authorization for an external-author PR. Only the protected
route's exact marker authorizes an external-author result. If evidence already exists, that route
posts a non-triggering approval marker instead of another request. Repository comment permissions
and provider access remain outside this action.

The beta template uses the repository `GITHUB_TOKEN`. Commit statuses and marked request comments
therefore carry the shared GitHub Actions identity, not a dedicated YAGA App. Consumers must keep
Actions defaults read-only and audit every same-repository workflow/integration able to obtain
`statuses: write` or `issues: write`. A same-named check cannot substitute for a required classic
status when GitHub requires both, but it can create ambiguity or denial of service. A write-capable
same-repository workflow remains inside the documented beta trust boundary.

Commit-status publication is not transactional, and neither is comment publication. Consumers must
require both native `Review Policy Boundary` and `CI Prerequisites` checks in addition to the two
classic YAGA statuses. Those native checks make lifecycle writer failure and failed/in-progress CI
merge-blocking even if an older classic success exists on the same SHA. YAGA performs bounded
before/after validation, reserves a terminal-error polling tail and lifecycle status capacity, and
never blindly retries an ambiguous request POST. GitHub or runner failure can still leave pending.
Recover by rerunning current CI or, near the status ceiling, pushing a new commit.

Comments, reviews, and reactions are each accepted only from one complete page. A page containing
100 records is ambiguous and fails closed; recovery requires a new PR. Commit-status history also
requires fewer than 100 visible records across every context and reserves two remaining visible
slots before terminal publication, independently of GitHub's longer per-SHA/context ceiling.
Recovery from either status bound requires a new commit.

An initial reaction-only success is accepted only for a non-draft `opened` boundary. On every later
boundary, including `ready_for_review`, a reaction is accepted only when it is strictly later than
the exact current YAGA request marker. Unrequested later reactions do not bind to the candidate.

Closure has no trigger and a merge push to `main` is filtered, so neither starts a new publisher.
A delayed invalidator reads the exact live PR and skips one already closed. An already-running
worker can still race its final live read with a subsequent comment or status POST; GitHub REST
cannot make those operations transactional. A marked review request or status may therefore land
after closure, and the request may consume provider quota. Compensating validation limits this
window but cannot guarantee zero post-close writes.

The beta trust model also assumes GitHub delivers each configured lifecycle event. A missed
same-head reopen or other lifecycle event can leave an older green result visible because no new
native `Review Policy Boundary` check is created. There is no schedule backstop; deployment must
canary event delivery. Ordinary metadata edits use `Review Policy Metadata`, which is intentionally
not required and cannot replace the required boundary check.

Supported versions will be listed after the first canary-backed release. Until then, pin the exact
audited commit SHA tested by the consumer repository.
