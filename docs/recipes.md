# Adoption recipes

These recipes are small starting points for common YAGA integrations. Keep policy files and
runtime selectors explicit as the repository grows.

## Start with local commit feedback

```bash
uv run yaga config init --repo .
uv run yaga config show --repo .
uv run yaga commit check --message "docs: explain YAGA"
```

`config init` creates only a new standalone `.yaga.toml` and refuses to overwrite or shadow an
existing discovered policy. For editor or tool integration, use JSON and rely on the exit code:

```bash
uv run yaga commit check --message "docs: explain YAGA" --format json > yaga-report.json
```

Exit `0` means pass, `1` means a valid input violated policy, and `2` means YAGA could not safely
complete the invocation.

For a status-only gate, add `--quiet`; policy output is suppressed but operational errors remain
visible on standard error:

```bash
yaga branch check --policy .yaga/branch-policy.toml --name feat/topic --quiet
```

## Enforce commit messages before creation

YAGA exposes one direct `commit-msg` pre-commit adapter. Pin the repository to an audited immutable
revision and install the non-default hook type:

```yaml
repos:
  - repo: https://github.com/dariuszpanas/yaga
    rev: <audited-40-character-commit-sha>
    hooks:
      - id: yaga-commit-check
```

```bash
pre-commit install --hook-type commit-msg --install-hooks
```

The hook checks the UTF-8 message file and uses the discovered commit policy. It is bypassable,
cannot inspect merge parents, and does not run for commits created through GitHub APIs. Keep a
complete-history CI check as the repository-side authority.

## Check a pull request's exact commits

For ordinary unprivileged CI, check out the event head with complete history and pass the exact
three-dot range through one quoted environment variable:

```yaml
- uses: actions/checkout@<audited-full-sha>
  with:
    ref: ${{ github.event.pull_request.head.sha }}
    fetch-depth: 0
    persist-credentials: false
- name: Check commit range
  env:
    YAGA_RANGE: ${{ format('{0}...{1}', github.event.pull_request.base.sha, github.event.pull_request.head.sha) }}
  run: >-
    uv run yaga commit check
    --range "$YAGA_RANGE"
    --format github
```

The three-dot range compares the unique merge base to the pull-request head and is evaluated
oldest first. YAGA does not fetch history. Pull-request CI is a quality signal, not trusted merge
authority.

## Require tests when source paths change

Use `change check` when the rule is about a delta rather than the final tree:

```toml
# .yaga/change-policy.toml
change-policy-version = 1

[[rules]]
name = "python-source-needs-tests"
when-any = ["src/**/*.py"]
require-any = ["tests/**/*.py"]
```

```bash
yaga change check \
  --policy .yaga/change-policy.toml \
  --range origin/main...HEAD \
  --format github
```

A rule is skipped when its trigger paths are absent, passes when a required path matches, and
reports a finding when the relationship is missing. Patterns are case-sensitive repository-
relative POSIX paths; there are no regexes, exclusions, or commands.

## Check the committed snapshot

Use a committed-tree provider when the requirement concerns what is actually in a revision, not
what happens to be staged or untracked locally:

```bash
yaga tree check --policy .yaga/tree-policy.toml --revision HEAD
yaga path check --policy .yaga/path-policy.toml --revision HEAD
yaga mode check --policy .yaga/mode-policy.toml --revision HEAD
yaga size check --policy .yaga/size-policy.toml --revision HEAD
```

Typical choices are `tree` for required and forbidden paths, `path` for Windows portability,
`mode` for regular/executable/symlink/gitlink modes, and `size` for per-blob and aggregate stored
byte budgets. These providers enumerate Git metadata only: they do not read tracked content,
inspect the index or worktree, follow gitlinks, or fetch missing objects. In CI, pass the exact
checked-out SHA through a quoted environment variable as `--revision`.

## Run one repeatable repository gate

Use a versioned plan when several independent providers should run together:

```toml
# .yaga/checks/ci.toml
plan-version = 2
checks = ["commit", "workflow", "workflow-security", "workflow-lint", "mode", "path", "size", "tree"]
workflow-paths = [".github/workflows", "examples"]
workflow-security-profile = "recommended-v3"
mode-policy = ".yaga/mode-policy.toml"
path-policy = ".yaga/path-policy.toml"
size-policy = ".yaga/size-policy.toml"
tree-policy = ".yaga/tree-policy.toml"
```

```bash
yaga repo check \
  --plan .yaga/checks/ci.toml \
  --commit HEAD \
  --revision HEAD \
  --format github
```

The plan selects providers but not runtime revisions. `--commit` and `--range` belong to the
commit provider; `--revision` is required by selected committed-tree providers. Plans are never
discovered, interpolated, executed, or trusted as security authority when changed by a pull
request. Reports remain separate, and exit `2` wins if any provider has an operational error.

## Add workflow supply-chain checks

```bash
yaga workflow check .github/workflows examples --format github
yaga workflow security .github/workflows examples --profile recommended-v3 --format github
yaga workflow lint .github/workflows examples --format github
```

`workflow check` enforces immutable references, `workflow security` enforces the selected trust
profile, and `workflow lint` runs pinned actionlint in a hardened network-disabled container. The
lint provider needs Docker; the other two are pure Python. A passing provider never replaces the
others.

## Diagnose without changing policy

```bash
yaga config show --repo . --format json
yaga commit check --repo . --commit HEAD --format json
yaga repo check --repo . --plan .yaga/checks/ci.toml --commit HEAD --revision HEAD --format json
```

If the result is exit `2`, check the explicit source, revision availability, repository history,
policy path, and output format. YAGA deliberately does not fetch, infer a branch or revision,
merge configuration files, or treat the current working tree as a committed snapshot.
