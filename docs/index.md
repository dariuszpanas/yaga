# YAGA

YAGA is an extensible Python CLI for explicit repository policy checks. It runs the same
bounded checks locally and in CI, with stable diagnostics and exit codes.

## What it checks

YAGA keeps policy providers separate so a repository can adopt only the checks it needs:

- Conventional Commit messages and pull-request titles
- branch names and changed-path relationships
- committed tree contents, blob sizes, paths, and entry modes
- GitHub Actions references, security policy, and workflow syntax
- an explicit aggregate repository plan
- the retained Codex review gate

Every provider is explicit about its source. YAGA does not fetch history, infer a working-tree
state, or silently combine inputs.

## Quick start

```bash
uv sync --group dev
uv run yaga --help
uv run yaga commit check --message "feat(cli): add a policy check"
```

Continue with [Get started](getting-started.md) for a first repository policy and the
[command reference](commands.md) for provider-specific invocations. The detailed
[commit policy](commit-policy.md), [repository checks](repository-checks.md), and
[GitHub Actions](github-actions.md) pages document the complete contract. See the
[changelog](changelog.md) for the user-facing release history.

## Project status

YAGA is pre-release. Pin the exact audited commit SHA when consuming its GitHub Action, and
read [SECURITY.md](https://github.com/dariuszpanas/yaga/blob/main/SECURITY.md) before enabling
the write-capable review gate.
