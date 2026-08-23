# YAGA repository agent guidance

These instructions apply to the whole repository. Read `CONTRIBUTING.md` before changing code or
opening a pull request.

## Start safely

Before editing, inspect:

```bash
git status --short
git branch --show-current
git log -1 --oneline
```

Preserve unrelated or concurrent work. Stage explicit paths and review both the working-tree and
cached diffs before reporting completion. Feature branches use `feat/<short-topic>` and fixes use
`fix/<short-topic>`.

## Preserve the trust boundary

- Treat event payloads, action inputs, API responses, comments, reviews, reactions, statuses, and
  workflow run metadata as untrusted until each required field is parsed and bounded.
- Keep the runtime dependency-free. A new runtime dependency needs an explicit security and
  maintenance rationale; developer-only tools belong in the `dev` dependency group.
- Never log or place `github-token` in an argument, output, status description, or exception.
- Keep read-only event observation separate from privileged default-branch publication. A fork or
  dependency-bot event must not execute its own code with a write-capable token.
- Fail closed on malformed, incomplete, ambiguous, stale, displaced, or over-budget evidence.
- Do not create review-request comments or use reaction state as proof for an exact commit unless a
  documented, race-safe binding exists.
- Centralize pinned GitHub App and actor identities. Do not make security identities configurable
  merely for convenience.
- Bound request counts, pagination, response bodies, request bodies, event files, candidate payloads,
  and all attacker-controlled strings.

## Make reviewable changes

- Keep generic GitHub, model, and status primitives under `src/yaga/`. Put gate-specific policy under
  a dedicated package such as `src/yaga/codex/`.
- Keep publisher state transitions separate from generic transport and parsing modules.
- Put focused tests in `tests/`.
- Add tests for success, malformed data, limits, stale identity, and failure behavior.
- Run `uv run make ci` before a push. It covers the locked dependency graph, Ruff, ty, the pinned
  actionlint container, tests, and a package build.
- Use Conventional Commit syntax for commits and pull-request titles.
- Pin every third-party action to an audited full commit SHA in example workflows.
