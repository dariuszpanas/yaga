# YAGA

YAGA is Yet Another Gate Action: reusable, conservative GitHub status gates.

Its first adapter turns observable Codex GitHub review outcomes into one
authoritative `Codex Review` commit status. It never creates `@codex review`
comments and never adds or removes reactions. Generic GitHub transport and
status primitives stay independent from Codex-specific evidence policy so
later gates can reuse the same trust boundary.

YAGA is pre-release. Do not consume an unpinned branch or tag; pin the audited
full commit SHA that your repository canaried.

## Project shape

`yaga.github`, `yaga.models`, and `yaga.status` provide bounded, gate-neutral
GitHub primitives. Each gate owns its candidate identity, evidence grammar,
provider identities, and state transitions in a dedicated package; the first
is `yaga.codex`. The public `gate` input is deliberately closed to implemented
adapters, so extensibility does not turn security-sensitive identities or
evidence rules into arbitrary workflow configuration.

## Why two consumer workflows?

GitHub event observation and status publication have different trust levels.
The observer runs with no token permissions, checks out nothing, uploads no
artifact, and records only bounded event metadata in its run title. A separate
default-branch `workflow_run` publisher receives the write token, validates the
source run and live pull request through GitHub's API, and executes only YAGA's
pinned code. This also gives Dependabot pull requests a write-capable publisher
without running dependency-branch code with that token.

The observer workflow name, the publisher's `workflow_run.workflows` entry, the
versioned `run-name` JSON, and `observer-workflow-path` input form one protocol.
If a consumer renames or changes one, it must update and canary the others in
the same change.

The repository ships templates in [`examples/`](examples/). Copy both files,
replace the all-zero action reference with an audited 40-character YAGA commit
SHA, and keep the observer path synchronized with the publisher input. The
publisher's jobs deliberately avoid the exact name `Codex Review`; that name
belongs only to the commit-status context required by a ruleset.

## Codex evidence contract

YAGA observes reviews; it does not initiate them. Configure Codex automatic
reviews in the repository, or have an owner make one deliberate `@codex review`
request when necessary. See OpenAI's [Codex GitHub review
documentation](https://learn.chatgpt.com/docs/third-party/github) for the
provider-side triggers and outcomes.

The adapter accepts only exact connector identities and these bounded forms:

- a clean connector issue comment from the official GitHub App with a reviewed
  commit marker that resolves to the current head;
- a formal connector findings review whose native `commit_id` and reviewed
  commit marker identify the current head; or
- the connector's `+1` reaction on the initial ready `opened` candidate.

A PR-body reaction has no commit or base identifier. YAGA therefore never
reuses a reaction for a later synchronized head. Reaction-only later reviews
remain pending until Codex emits commit-bound evidence. A base change also
remains pending until the pull request moves to a new head because the
connector does not expose the reviewed base or review-start identity.
In particular, a draft-to-ready review that produces only a `+1` reaction stays
pending: GitHub exposes the reaction completion time, but not the Codex
review-start time or exact candidate identity needed to bind it safely.

GitHub schedules may be delayed or dropped. The thirty-minute schedule is a
repair/backstop, not a completion-time promise; uncertainty leaves the status
pending. Native GitHub required conversation resolution is a v1 deployment prerequisite:
a formal Codex findings review completes YAGA's review status, while GitHub must
continue blocking the pull request until every findings thread is resolved.

Each scheduled pass first scans all supported open pull requests and writes
pending for every detected lost, newer, or base-mismatched lifecycle boundary.
Only after that repair phase may it select a rotating window of at most four
already-pending candidates for terminal evidence reconciliation. With the
shipped limits, the scheduled path is modeled at no more than 710 GitHub REST
requests per hour: two 163-request repair passes plus four 48-request terminal
jobs per pass. This is a scheduled-work estimate, not a total repository
guarantee under arbitrary event traffic; rate-limit or API uncertainty keeps an
already-observed boundary pending. Repositories with more than 40 open pull
requests are rejected by the bounded beta repair pass. The 1,000-request/hour
public-repository `GITHUB_TOKEN` basis is documented in GitHub's
[REST API rate-limit guidance](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

## Safe rollout

Merge the pinned observer and publisher workflows before adding `Codex Review`
as a required context. `workflow_run` only uses workflows present on the
default branch, so a bootstrap pull request cannot prove its own new publisher.
After merge, use a ready canary pull request to verify the exact status context,
clean-review behavior, and close behavior. Require the context only after that
canary succeeds.

YAGA v1 requires `Codex Review` to be an up-to-date/strict required status.
Merge queues are unsupported in v1: the shipped workflows have no `merge_group`
trigger or combined-head contract. A default-branch tip can advance without
emitting a pull-request lifecycle event, while commit statuses are scoped only
to the head SHA. Scheduled pending repair is defense in depth for missed
observer deliveries and shared heads; it is not a substitute for strict branch
currency.

GitHub commit-status writes are not transactional. YAGA reserves enough local
request budget and wall-clock window before publishing success to run its
bounded validation and compensating-pending tail. The shipped 15-minute
terminal jobs run YAGA as their first and only step, declare that same window
through `job-timeout-minutes`, and reserve an additional cleanup margin. Custom
consumers must preserve that layout; preceding steps are unsupported unless the
GitHub job timeout exceeds the declared YAGA window by their complete budget.
The REST timeout bounds socket inactivity rather than total response time, and
runner termination remains external to the process. If GitHub accepts success
and a network, rate-limit, trickled-response, or runner outage then rejects or
prevents every repair write, that status can remain ambiguous until a later
event or scheduled repair succeeds. Likewise, a new PR can briefly inherit a
status on a reused SHA before its asynchronous observer boundary is published.
Privileged merge automation must wait for the current PR-specific YAGA boundary
instead of acting on inherited green immediately.

YAGA stops terminal reconciliation after 100 entries in the case-insensitive
status context while continuing to reserve the remaining capacity for
fail-closed invalidation. GitHub permits at most 1,000 statuses for one SHA and
context; pathological same-SHA churn therefore requires a new head before the
gate can publish another terminal result.

The templates use the repository `GITHUB_TOKEN`, so their status creator is the
shared GitHub Actions integration rather than a YAGA-specific App. Before
requiring the context, reserve the exact `Codex Review` name: audit every
default-branch workflow and integration with `statuses: write`, and remove any
other check or publisher that can emit that context. GitHub compares commit
status contexts case-insensitively, so this audit must reserve every
case-insensitive alias and must reject colliding workflow, job, or check names;
see GitHub's [commit-status API contract](https://docs.github.com/en/rest/commits/statuses?apiVersion=2022-11-28).
YAGA trusts the consumer's write-capable default-branch Actions configuration.
A dedicated publisher App identity is a possible stronger boundary for a later
release.

The shared observer includes `closed` so other gates can recover shared-head
ownership. The Codex publisher filters that event before token-backed work and
also treats it as a defensive runtime no-op, so it never writes or requests a
post-merge review. A `converted_to_draft` transition instead persists pending
without starting terminal review work; a later ready event must establish a
new review boundary.

The shipped workflows target GitHub-hosted Ubuntu runners and require Bash and
Python 3.12 or newer. The first Codex contract handles pull requests targeting
the repository's default branch; other base branches remain ineligible. Other
runner families and base-branch trust policies are not supported by this beta.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and action-contract
details. Report suspected vulnerabilities through the private process in
[SECURITY.md](SECURITY.md).
