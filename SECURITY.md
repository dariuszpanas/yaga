# Security policy

YAGA contains both a local/CI repository-policy CLI and security-sensitive GitHub Actions
infrastructure. Do not open a public issue for a suspected vulnerability. Use GitHub private
vulnerability reporting and include the affected commit, execution surface, expected fail-closed
behavior, and a secret-free reproduction.

Useful reports include:

- shell or option injection through a commit revision, repository path, configuration, or commit
  message;
- terminal escape/control injection, unbounded message/config/Git output, or JSON contract
  confusion in `commit check`;
- the write-capable root Action importing Typer, site packages, commit-policy modules, or any
  dependency installed at runtime;
- the read-only commit runtime accepting a token, GitHub API or Git-fetch path, caller-selected
  revision, synthetic merge checkout, shallow history, unbound event/repository identity, or
  unescaped workflow command;
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

## Local and CI CLI boundary

Commit messages and Git output are untrusted. The checker bounds input and selection sizes,
sanitizes displayed text, invokes Git without a shell, rejects option-like or split revision
expressions, and fails when requested history is absent or incomplete. It reads no token and makes
no network request. A selected `.yaga.toml` or `pyproject.toml` is trusted repository policy, but its
shape, types, token lengths, patterns, and schema version are still validated strictly so a typo
cannot silently weaken checks.

The installed CLI depends on locked Typer packages. The write-capable root Action does not: its fixed
`YAGA_ACTION_RUNTIME=1` path runs with Python site packages disabled and enters only the
standard-library Action/gate import graph. Both frontends call the same gate dispatcher. The GitHub
token stays in `GITHUB_TOKEN`; no gate command accepts it as an argument or configuration value.

The separate commit Action uses a pinned Python bootstrap with an explicit empty token; that
bootstrap may obtain Python before YAGA starts. Its dependency-free YAGA runtime uses a different
`YAGA_COMMIT_ACTION_RUNTIME=1` selector and may enter only bounded commit/event/reporting modules.
The runtime has no token input, GitHub API client, Git fetch behavior, or write code path; the
reference workflow grants only `contents: read`. It binds a strict `pull_request` event to the
runner repository ID/name and refs, requires the worktree at the exact event head, rejects shallow
or missing history, and emits at most 50 escaped annotations. Its configuration comes from the
unprivileged PR head, so this result is a reviewable quality signal rather than tamper-proof merge
security. Normal consumers pin the Action code; YAGA's local dogfood workflow deliberately executes
the PR copy without granting write permissions or secrets.

## Codex review gate boundary

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
canary before relying on it. Disable Codex automatic reviews before enabling the v2 publisher, then
prove the owner request and protected-external approval plus request paths before allowing normal
contributor traffic.

The cancel-stale per-PR `authorize-external` job records only a non-triggering approval marker. A
separate PR-serialized request worker does not cancel an in-flight run and may post `@codex review`
only after revalidating that marker. This split keeps a stale approval wait from blocking a newer
boundary without allowing cancellation to duplicate a request.

The environment protects only YAGA's request token. It cannot stop a person or other integration
from directly posting `@codex review`; provider-side execution may still consume quota. YAGA does
not accept that unsolicited activity as pending or successful evidence. When it is visible, YAGA
fails closed without posting a duplicate request. Only the protected route's exact approval marker
authorizes YAGA to request review for an external-author PR; external approval never reuses
unsolicited evidence. Repository comment permissions and provider access remain outside this action.

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

Immediately before its request POST, YAGA revalidates the candidate and approval and re-reads the
exact request plus admissible pending/outcome evidence. The final read and POST are not atomic, so
another actor can still start a review in that interval. Disabling automatic reviews removes the
normal provider-side source of that residual duplicate-request race; repository comment permissions
and provider access remain separate controls. Codex outcomes do not identify their triggering
comment, so a direct human/app request in that interval can consume quota and later look temporally
correlated with YAGA's marker. The action cannot eliminate that provider-side ambiguity.

Comments, reviews, and reactions are each accepted only from one complete page. A page containing
100 records is ambiguous and fails closed. More than eight older YAGA request boundaries also fail
closed; recovery from either request-history condition requires a new PR. Commit-status history
requires fewer than 100 visible records across every context and reserves two remaining visible
slots before terminal publication, independently of GitHub's longer per-SHA/context ceiling.
Recovery from either status bound requires a new commit.

Every accepted eyes reaction or outcome, including on `opened`, must be strictly later than the
exact current-boundary Actions-owned YAGA request marker. The `observe` route is available only for
that already-posted exact request. Visible unsolicited connector activity fails closed without a
duplicate YAGA request. A PR-body reaction has no commit/base identity, and reviewed-commit evidence
still does not identify its triggering comment, so the marker supplies temporal correlation rather
than a native provider binding. Disable and drain automatic reviews before activation, and prevent
direct or integration-triggered reviews from overlapping YAGA. A repository that cannot enforce
that invariant must not enable this beta action. YAGA refuses a newer evidence/request path unless
every older YAGA request has trusted successful status lineage. If a head changes while its request
is unresolved or timed out, recovery requires a fresh PR.

Closure has no publisher trigger. A post-merge CI completion has event `push`, so every publisher
job skips before YAGA runs. A delayed invalidator reads the exact live PR and skips one already
closed. An already-running worker can still race its final live read with a subsequent comment or
status POST; GitHub REST cannot make those operations transactional. A marked review request or
status may therefore land after closure, and the request may consume provider quota. Compensating
validation limits this window but cannot guarantee zero post-close writes.

The beta trust model also assumes GitHub delivers each configured lifecycle event. A missed
same-head reopen or other lifecycle event can leave an older green result visible because no new
native `Review Policy Boundary` check is created. There is no schedule backstop; deployment must
canary event delivery. Ordinary metadata edits use `Review Policy Metadata`, which is intentionally
not required and cannot replace the required boundary check.

Supported versions will be listed after the first canary-backed release. Until then, pin the exact
audited commit SHA tested by the consumer repository.
