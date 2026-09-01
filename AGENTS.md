# YAGA repository agent guidance

Read `CONTRIBUTING.md` before changing code or opening a pull request. Start with `git status
--short`, `git branch --show-current`, and `git log -1 --oneline`; preserve unrelated work, stage
explicit paths, and inspect both working and cached diffs. Feature branches use `feat/<topic>` and
fixes use `fix/<topic>`.

YAGA is an extensible CLI for checks that work locally and in CI. Keep the Conventional Commit
checker under `src/yaga/commits/`, installed Typer commands under `src/yaga/commands/`, and the
retained Codex review gate under `src/yaga/codex/`. Do not discard the gate experiment: expose its
operations through `yaga gate` and keep the composite Action as a supported adapter.

## Preserve the CLI contract

- `commit check` accepts one explicit source or defaults to `HEAD`; never silently combine sources,
  fetch history, reduce a Git selection, or ignore a missing base.
- Treat commit messages, Git output, revisions, paths, and displayed labels as untrusted. Bound
  inputs and output, invoke Git without a shell, reject option-like/split revisions, and sanitize
  terminal text.
- Load exactly one nearest `.yaga.toml` or `pyproject.toml`, or one explicit `--config`; reject
  unknown keys, invalid types, duplicate normalized tokens, and unsupported schema versions.
- Keep parser structure separate from configurable policy. Stable diagnostic codes, exit codes
  0/1/2, and the versioned JSON document are public pre-release contracts.
- The installed CLI may use the locked Typer dependency. The composite Action may not import Typer,
  CLI command modules, commit-policy modules, Rich, Click, or site packages. Preserve the fixed
  dependency-free `YAGA_ACTION_RUNTIME=1` bootstrap and shared gate dispatcher.

## Preserve the trust boundary

- Treat events, inputs, API responses, comments, reviews, reactions, statuses, and run metadata as
  untrusted until strictly parsed and bounded.
- Keep the composite-Action import graph dependency-free and never expose `github-token` in
  arguments, output, status, logs, or exceptions.
- Keep PR CI unprivileged. Write-capable YAGA operations run only from trusted default-branch
  `pull_request_target` or `workflow_run` workflows and never consume PR code, artifacts, or cache.
- Treat PR CI as a quota-saving heuristic, not a security authority. Merge security also relies on
  maintainer approval, required review, and audited write-capable workflows/integrations.
- Require the native lifecycle check, then validate live state and status capacity before lifecycle
  pending writes. Request and terminal paths revalidate live state after any queue/environment wait
  and immediately around their write.
- Keep external authorization split: cancel stale per-PR `authorize-external` waits, record only a
  non-triggering protected approval marker, then let the separately PR-serialized request worker run
  with `cancel-in-progress: false`. Only the literal protected `codex-review-approval` route may
  authorize a non-owner PR.
- Require repository variable `YAGA_CODEX_OWNER_ID`. The environment must have that owner as sole
  required reviewer, prevent self-review, disable admin bypass, store no secrets, and provide
  `YAGA_CODEX_APPROVAL_MARKER=codex-review-approval:v1`. Verify plan support and canary behavior.
- Require the exact current-boundary Actions-owned YAGA request before every accepted eyes reaction
  or outcome, including `opened`, with evidence strictly later than the request. Visible unsolicited
  connector activity fails closed without a duplicate request, and `observe` requires that request.
- Fail closed on malformed, incomplete, ambiguous, stale, displaced, or over-budget evidence.
- Bound pagination, responses, request bodies, event files, descriptions, polling, and all
  attacker-controlled strings.
- Treat 100 comments, reviews, or reactions as incomplete and require a new PR. Require a new commit
  before 100 total visible statuses across all contexts or the per-SHA/context status ceiling.
- Reserve `Codex Review` and `CI Gate` for classic statuses; workflow/job/check names must differ.
  Audit every `statuses: write` and `pull-requests: write` path because the beta uses the shared
  Actions identity for both statuses and PR conversation comments.
- Preserve both authenticated `CI` and `YAGA Review Policy` completion wakes, deterministic
  later-completion election before writes (CI wins timestamp ties), the two-minute CI-first prepare
  wait, and exact workflow-path/event entry guards. Lifecycle completions on the `main` base branch
  must run, while post-merge `push` completions skip every publisher job before YAGA runs.
- Disable automatic reviews before enabling the v2 publisher. Prove the owner request and
  protected-external approval plus request paths before normal contributor traffic. Freeze new PRs,
  reach zero open PRs, and drain existing Codex tasks before activation; all evidence correlation
  also requires preventing overlapping direct or integration-triggered reviews and trusted success
  lineage for every older YAGA request.
- External approval authorizes only the exact YAGA request and never reuses unsolicited evidence.
- Ordinary metadata edits use non-required `Review Policy Metadata`; real lifecycle events use
  required `Review Policy Boundary`. The beta assumes GitHub delivers those configured events.
- Never promise zero post-close writes: close can race the last live read and a comment/status POST.
  YAGA cannot stop direct human/app `@codex review` comments from consuming provider quota.

## Make reviewable changes

Keep generic GitHub/model/status primitives under `src/yaga/` and gate policy under its provider
package. The public operations include `authorize`; `prepare` emits closed `route` values including
`approved` plus `pull_request_number`. Prefer focused modules and tests. Pin every third-party
example action to an audited full SHA. Run `uv run make ci` before push; it covers locked
dependencies, Ruff, ty, pinned actionlint, tests, and package build. Use Conventional Commits and
fold review/CI fixes into the logical commit.
