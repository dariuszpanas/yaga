# Contributing

YAGA is an extensible repository-policy CLI with a security-sensitive GitHub Action surface. Keep
changes focused, test-backed, and explicit about the public command, configuration, diagnostic, or
gate boundary they affect. The first local/CI policy is `commit check`; the retained write-capable
gate is `codex-review`.

## Development

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are recommended:

```bash
uv sync --group dev
uv run make ci
```

The full gate uses Docker for the pinned actionlint container. The installed CLI uses the locked
Typer dependency. The write-capable root Action import graph—`__main__` in gate mode, `action_cli`,
`action`, `codex`, and the generic GitHub primitives—must remain Python-standard-library-only and
must not import the installed CLI, command, commit-policy, or presentation modules. The separate
read-only commit Action may import dependency-light commit modules, but never Typer, command
modules, Codex policy, GitHub REST transport, site packages, or token handling.

The package build gate must execute the fresh wheel outside the checkout. Export the publishable
runtime closure from `uv.lock`, require its hashes without building dependencies, install the wheel
with dependency resolution disabled, run `uv pip check`, and execute the installed `yaga` entrypoint
with project and Python environment leakage removed.

## CLI and commit policy contract

The public installed command groups are `commit`, `config`, `github`, and `gate`. Keep Typer
declarations in `src/yaga/commands/`; keep commit parsing, policy, Git selection, configuration,
GitHub event adaptation, and reporting in focused dependency-light modules under
`src/yaga/commits/`. Domain behavior must remain directly testable without invoking Typer.

`commit check` accepts exactly one of a message, UTF-8 file, standard input, Git commit, or Git
range; with none it checks `HEAD`. Preserve full messages, deterministic oldest-first range order,
hard message/config/output/count bounds, shell-free Git invocation, and explicit failure for missing
or shallow history. Commit messages and Git output are untrusted terminal input: sanitize and bound
anything displayed. The installed entrypoint must configure UTF-8 standard output and error before
Typer renders user-controlled text; keep the legacy-console-encoding subprocess regression.

Configuration is schema version 1 in `[tool.yaga]` plus `[tool.yaga.commit]`, or in the standalone
`.yaga.toml` root plus `[commit]`. Load exactly one nearest or explicit file, reject unknown keys and
wrong types, and do not silently merge policies. Stable diagnostic identifiers and JSON schema
fields are public pre-release interfaces; change them deliberately and test both text and JSON.
`config init` creates only a new standalone `.yaga.toml` with exclusive no-overwrite semantics. It
must refuse to shadow any effective discovered configuration, never edit `pyproject.toml`, and keep
its deterministic starter template round-trippable through the strict loader.

The pre-commit provider manifest exposes exactly one `commit-msg` hook. Keep it as a direct
`language: python` adapter to `yaga commit check --file`; do not add shell indirection, filename
filters, duplicated policy arguments, or extra dependencies. Validate the manifest and exercise a
real `pre-commit try-repo` installation when changing the hook or package metadata. Local hooks are
bypassable and cannot determine merge parent count, so they complement rather than replace CI range
checks.

The write-capable root Action uses the same `gate codex-review <operation>` command path through a fixed
`YAGA_ACTION_RUNTIME=1` standard-library bootstrap. It installs no package and makes no network
request for dependencies. Never accept `github-token` as argv, configuration, output, or logs.

The read-only Action uses its own closed `YAGA_COMMIT_ACTION_RUNTIME=1` bootstrap and the public
`github pull-request check` path. It accepts no token or caller-selected revisions, validates one
bounded event against the GitHub runner repository/ref identity, requires `HEAD` to equal the event
head, and never fetches Git history. Keep the pinned Python bootstrap token explicitly empty, and
keep its `edited`-aware workflow separate from the authenticated `CI` workflow used by the Codex
gate. Its PR-head configuration is contributor-controlled and is only a quality signal.

## Action contract

The public inputs are exactly `gate`, `operation`, `github-token`, `prerequisite-workflow`,
`lifecycle-workflow`, `owner-id`, `approval-marker`, `request-timeout`, and `job-timeout-minutes`.
`operation` is closed to `invalidate`, `prepare`, `authorize`, `observe`, `request`, and `finalize`.
Only `prepare` emits the closed `route` capability and `pull_request_number` used by the template;
its routes are `skip`, `done`, `observe`, `owner`, `external`, and `approved`. Candidate identity
never crosses a job output.

YAGA is pre-release; remove obsolete interfaces rather than adding compatibility shims. Provider
identity, status names, polling cadence, and marker grammar are fixed policy rather than caller
inputs. Add a future gate as a separate package with its own closed evidence grammar.

## Consumer trust split

- The `pull_request_target` invalidator's native `Review Policy Boundary` check must be required.
  It verifies exact live state and status capacity, then writes pending for `Codex Review` and
  `CI Gate`. It excludes `closed`, executes no PR code, and posts no comment. Ordinary title/body
  edits instead use the non-required native name `Review Policy Metadata`.
- PR-controlled CI remains unprivileged and ends at `CI Prerequisites`. Its exact bounded run-name
  is `YAGA CI <action> for #<pull-request> at base <full-base-SHA>`, and its pull-request triggers
  are `opened`, `synchronize`, `reopened`, and `ready_for_review`. This PR-controlled result is an
  untrusted quota-saving prerequisite/heuristic, not a security authority.
- The `workflow_run` publisher accepts authenticated completions from both `CI` and
  `YAGA Review Policy`, then resolves the exact current CI/lifecycle pair. It uses no upstream
  artifact or cache. A CI-first `prepare` waits at most two minutes for lifecycle publication, and
  a later lifecycle completion is a second reconcile wake. The later completion is elected before
  any write, with CI winning an exact timestamp tie. Failed CI never requests Codex.
- `prepare` may observe only evidence following an exact current-boundary YAGA request and cannot
  comment. `request-owner` is direct.
  `authorize-external` uses the literal protected environment and records a non-triggering approval
  marker. The separately serialized `request-external` worker consumes that capability and never
  cancels an in-flight request. `observe` never comments. `finalize` independently revalidates
  source CI, live identity, authorization, Codex evidence, and status lineage.
- Every job invokes the pinned action as its sole step with the narrow permissions in `examples/`.

Consumers provide the immutable owner ID through required repository variable
`YAGA_CODEX_OWNER_ID`. Precreate `codex-review-approval` with that owner as the sole required
reviewer, prevent self-review, disable administrator bypass, store no secrets, and add environment
variable `YAGA_CODEX_APPROVAL_MARKER=codex-review-approval:v1`. Required-reviewer protection is
plan-limited: verify the public-repository supported plan or private/internal Enterprise scope and
canary the wait before enabling it. Disable Codex automatic reviews before enabling the v2
publisher, then prove the owner request and protected-external approval plus request paths before
admitting normal contributor traffic.
The environment controls only YAGA: a direct human/app `@codex review` comment can still consume
provider quota. Visible unsolicited activity fails closed without a second YAGA request, but a
direct request can still race the final read/POST interval and create temporal ambiguity.

The publisher admits `pull_request` completions only from `.github/workflows/ci.yml` and
`pull_request_target` completions only from `.github/workflows/review-policy.yml`. That admits the
lifecycle wake associated with the default branch while post-merge `push` completions skip every
publisher job before YAGA runs. It also permits a fork head branch named `main`. There are no
schedule, issue-comment, review, merge-queue, or close triggers. A metadata-only edit has a unique
concurrency group and cannot cancel an active boundary. Consumers with another default branch need
to replace only `branches: [main]` in the lifecycle workflow. Keep the `CI` and `YAGA Review Policy`
display names in the publisher trigger synchronized with their authenticated paths.

## Design rules

- Treat event payloads, API responses, comments, reviews, reactions, statuses, and workflow runs as
  untrusted until every required field is parsed and bounded.
- Rely on the required native lifecycle check while validating live state and capacity before
  lifecycle pending writes. Before comments and terminal statuses, revalidate exact current CI,
  default branch, open/ready PR, head/base, unique ownership, lifecycle provenance, authorization,
  exact evidence capability, and latest status lease.
- Accept only exact connector identities and closed outcome grammars. Every eyes reaction and
  outcome, including on `opened`, requires the exact current-boundary Actions-owned YAGA request and
  a strictly later timestamp. `observe` is available only for that already-posted request. Visible
  unsolicited activity fails closed without a duplicate request. A PR-body reaction has no commit
  or base identity, and reviewed-commit evidence does not identify its triggering comment, so the
  request marker provides temporal correlation rather than a native provider binding. Require
  trusted successful status lineage for every older YAGA request before newer evidence or a request;
  a head changed while review is unresolved requires a fresh PR. Do not enable this beta action
  where automatic reviews were not disabled and drained or another review trigger can overlap YAGA.
- External authors require a YAGA request created after environment approval. Approval never reuses
  unsolicited evidence. Immediately before posting, revalidate and re-read the request and
  admissible evidence. The final read/POST interval is not atomic. CI reruns reuse one
  lifecycle-bound request.
- Ordinary metadata edits are no-ops; base edits and drafts revoke inherited success. Closure has
  no publisher trigger, while a post-merge `push` completion skips every publisher job before YAGA
  runs. An already-running job can still race its final live read with a comment or status POST, so
  a marked request/status can land after close and the request can consume quota. Treat this as a
  bounded residual, not a zero-post-close guarantee.
- Fail closed on malformed, incomplete, ambiguous, stale, displaced, or over-budget state. Shared
  heads require distinct commits. A comments, reviews, or reactions page with 100 records requires a
  new PR, as does more than eight older YAGA request boundaries; reaching the status-history ceiling
  requires a new commit.
- Bound request counts, pagination, bodies, event files, descriptions, polling, and all
  attacker-controlled strings. Never log the token or place it in arguments/outputs.
- Reserve `Codex Review` and `CI Gate` for classic statuses. Audit all `statuses: write` and
  `pull-requests: write` workflows because the beta uses the shared Actions identity.

Strict up-to-date `Review Policy Boundary`, `CI Prerequisites`, `Codex Review`, and `CI Gate`
requirements plus required conversation resolution are consumer prerequisites. Merge queues are
unsupported. Merge security also relies on `Maintainer Approval` and audited status/comment writers;
the protected environment and owner ID secure quota authorization. This beta assumes GitHub
delivers every configured lifecycle event, because there is no scheduled repair for a missed
same-head transition.

## Testing and pull requests

Add parser/checker tests for every new commit rule, strict configuration tests for every key,
shell-free Git integration tests for selection behavior, and Typer runner tests for command/exit
contracts. Keep a subprocess smoke proving `python -P -S -m yaga gate codex-review ...` reaches the
dependency-free Action boundary without importing Typer.

Keep generic transport/models/status primitives in `src/yaga/` and provider policy in
`src/yaga/codex/`. Add focused tests for success, failed CI, direct/protected routing, unsolicited
external outcomes, exact request idempotency, malformed/incomplete APIs, deadline reserves,
draft/ready/base/close races, same-head ambiguity, run supersession, status lineage, and
case-insensitive collisions. Repository-contract tests pin public inputs, outputs, permissions,
triggers, fixed environment names, action SHA, and absence of legacy writers.

Run `uv run make ci` before every push. Use Conventional Commit subjects. Preserve behavior,
motivation, security boundary, failure mode, migration impact, and validation in the retained commit
and PR. Fold review fixes and CI repairs into the logical commit they correct.
