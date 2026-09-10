# Get started

YAGA supports Python 3.12 or newer. The repository uses `uv` to keep the installed CLI,
development tools, and documentation builder reproducible.

## Install from a checkout

```bash
uv sync --group dev
uv run yaga --help
```

The package distribution is `yaga-cli`; the executable and import package are both `yaga`.

## Create a starter policy

From a repository that does not already have a discovered policy:

```bash
uv run yaga config init --repo .
uv run yaga config show --repo .
```

Initialization creates only `.yaga.toml` and refuses to overwrite an existing discovered policy.
Policy discovery selects one nearest `.yaga.toml` or `pyproject.toml`; use `--config` when the
source must be explicit.

## Check a commit

With no source option, `commit check` checks `HEAD`. Otherwise select exactly one source:

```bash
uv run yaga commit check
uv run yaga commit check --message "feat(cli): add a check"
uv run yaga commit check --file .git/COMMIT_EDITMSG
uv run yaga commit check --range origin/main..HEAD
```

The stable exit contract is:

| Exit | Meaning |
| --- | --- |
| `0` | All checked records passed. |
| `1` | A policy finding was reported. |
| `2` | Input, configuration, repository, or Git operation failed. |

Use `--format json` for a versioned machine-readable report and `--format github` for escaped
workflow annotations.

## Add the commit-message hook

YAGA exposes one direct `commit-msg` adapter through `.pre-commit-hooks.yaml`:

```yaml
repos:
  - repo: https://github.com/dariuszpanas/yaga
    rev: <audited-40-character-commit-sha>
    hooks:
      - id: yaga-commit-check
```

Install that hook type explicitly:

```bash
pre-commit install --hook-type commit-msg --install-hooks
```

The hook complements CI; it cannot inspect merge parents or replace a complete-history range
check.

## Build these docs locally

The documentation site is managed by [Zensical](https://zensical.org/docs/get-started/):

```bash
make docs
make docs-serve
```

`make docs` performs a clean strict build. `make docs-serve` starts the local preview server at
`http://localhost:8000`.
