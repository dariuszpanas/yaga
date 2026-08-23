# YAGA repository agent guidance

Read `CONTRIBUTING.md` before changing code or opening a pull request. Start with `git status
--short`, `git branch --show-current`, and `git log -1 --oneline`; preserve unrelated work, stage
explicit paths, and inspect both working and cached diffs. Feature branches use `feat/<topic>` and
fixes use `fix/<topic>`.

## Preserve the trust boundary

- Treat events, inputs, API responses, comments, reviews, reactions, statuses, and run metadata as
  untrusted until strictly parsed and bounded.
- Keep runtime code dependency-free and never expose `github-token` in arguments, output, status,
  logs, or exceptions.
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
- Accept an unrequested reaction-only result only for an initial non-draft `opened` boundary. Every
  later reaction, including `ready_for_review`, requires the exact request and a strictly later
  timestamp.
- Fail closed on malformed, incomplete, ambiguous, stale, displaced, or over-budget evidence.
- Bound pagination, responses, request bodies, event files, descriptions, polling, and all
  attacker-controlled strings.
- Treat 100 comments, reviews, or reactions as incomplete and require a new PR. Require a new commit
  before 100 total visible statuses across all contexts or the per-SHA/context status ceiling.
- Reserve `Codex Review` and `CI Gate` for classic statuses; workflow/job/check names must differ.
- Preserve both authenticated `CI` and `YAGA Review Policy` completion wakes, deterministic
  later-completion election before writes (CI wins timestamp ties), the two-minute CI-first prepare
  wait, and the `main` branch filter that prevents a post-merge publisher wake.
- Keep automatic reviews enabled through the initial canary; disable them only after YAGA's request
  path succeeds, then repeat owner and protected-external canaries.
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
