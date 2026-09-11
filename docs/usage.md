# Usage modes

YAGA is designed to be used at different points in a development lifecycle. The same provider
rules and stable reports can run interactively, in local hooks, in ordinary CI, or through one of
the supplied Actions.

## Interactive CLI

Use the installed command when a developer is diagnosing one input:

```bash
yaga commit check --message "feat(cli): explain a policy"
yaga config show --format text
yaga workflow security .github/workflows/ci.yml
```

The command writes human-readable findings to standard output. Invocation, configuration, Git,
and other operational errors use standard error and exit `2`; policy findings use exit `1`.

The standalone policy-check commands `commit check`, `branch check`, `change check`, `mode check`,
`path check`, `size check`, and `tree check` accept `--quiet` when a caller needs only the exit
status. `commit quality` accepts the same option for advisory findings. Quiet mode suppresses
policy or advisory reports on standard output; operational errors still use standard error and
exit `2`. Use `--format json` when another program needs the versioned document.
The same providers, plus `workflow check`, `workflow security`, `workflow lint`, and `repo check`,
support `--format github` for escaped workflow annotations where documented. Configuration reports
remain text/JSON; `commit quality` also supports GitHub warning/error annotations, while `gate
agent-review policy evaluate` additionally supports GitHub output for adapter result evaluation.
For multi-lens adapters, `agent-review policy template` emits a complete pending JSON receipt
scaffold whose digest and lens entries can be filled as provider work completes.

## Explicit commit sources

Commit checking has one deliberate source-selection contract:

```bash
yaga commit check --message "feat(cli): explain a policy"
yaga commit check --file .git/COMMIT_EDITMSG
yaga commit check --stdin < message.txt
yaga commit check --commit HEAD~1
yaga commit check --range origin/main..HEAD
```

With no source option, it checks `HEAD`. Supplying two sources is an operational error. A range
preserves deterministic oldest-first order. Git selection never fetches, does not use a shell, and
requires complete history when parent relationships matter.

## Local pre-commit hook

The hook is a thin direct adapter around `yaga commit check --file`; policy remains in the
repository configuration:

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

It catches malformed messages before they are committed, but it is bypassable and cannot know a
merge commit's parent count. Keep the complete-history CI check as the authoritative repository
guard.

## Ordinary CI

CI should install a pinned environment, check out complete history, and pass event-derived values
through quoted environment variables:

```yaml
- uses: actions/checkout@<audited-full-sha>
  with:
    fetch-depth: 0
    persist-credentials: false
- name: Check the pull request range
  env:
    YAGA_RANGE: ${{ format('{0}...{1}', github.event.pull_request.base.sha, github.event.pull_request.head.sha) }}
  run: >-
    uv run yaga commit check
    --range "$YAGA_RANGE"
    --format github
```

Do not interpolate untrusted branch names, commit messages, paths, or event fields into shell
source. Pull-request CI is an unprivileged quality signal; it is not trusted merge authority.

## Aggregate repository plan

Use a plan when a repository wants one repeatable set of independent checks:

```bash
yaga repo check \
  --plan .yaga/checks/ci.toml \
  --commit HEAD \
  --revision HEAD \
  --format github
```

The plan chooses providers; runtime options still choose the commit and revision. Plan v1 contains
the original commit/workflow providers. Plan v2 can add the committed-tree providers. There is no
implicit `all`, automatic plan discovery, or provider-specific argument when that provider was not
selected. Exit `2` wins if any selected provider has an operational error, even when another has a
policy finding.

## Read-only commit Action

The read-only Action validates one GitHub pull-request event, the runner repository/ref identity,
the checked-out head, configuration, and the exact commit range. It can apply the opt-in
Dependabot skip only after all validation succeeds. It never accepts a caller-selected token or
revision and never runs the Agent review gate.

## Write-capable review gate

The retained gate is a separate trusted workflow adapter:

```bash
yaga gate agent-review prepare
yaga gate agent-review request
yaga gate agent-review finalize
```

Consumers should use the supplied composite Action and trusted default-branch workflow model,
not invoke write operations from pull-request code. See [Agent review gate](agent-review-gate.md)
for the lifecycle, evidence, authorization, and quota requirements.

## Documentation preview and build

```bash
make docs-serve
make docs
```

The preview server watches Markdown changes. The build target uses Zensical strict clean mode so
stale cache state cannot hide broken links or navigation.
