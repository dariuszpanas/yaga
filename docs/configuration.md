# Configuration

YAGA configuration is intentionally explicit and versioned. A configuration file is policy, not
runtime input: it must not contain commands, environment interpolation, secrets, or provider
selection side effects.

## File discovery

For commit policy, choose one of these shapes:

```toml
# pyproject.toml
[tool.yaga]
config-version = 1

[tool.yaga.commit]
scope-policy = "optional"
```

```toml
# .yaga.toml
config-version = 1

[commit]
scope-policy = "optional"
```

YAGA walks from the requested directory toward the repository root and loads exactly one nearest
`.yaga.toml` or `pyproject.toml`. A same-directory `.yaga.toml` wins. `--config` selects one
explicit file and disables discovery; files are never merged. Unknown keys, wrong TOML types,
duplicate normalized values, unsafe paths, and unsupported versions are errors.

## Commit policy keys

| Key | Values and purpose |
| --- | --- |
| `allowed-types` | Optional nonempty ordered type tokens. Omit to allow any valid type. |
| `type-case` | `any`, `lower`, or `upper`. |
| `scope-policy` | `optional`, `required`, or `forbidden`. |
| `scope-policy-by-type` | Up to 128 type-to-presence overrides; each replaces the global scope policy. |
| `allowed-scopes` | Optional unique scope tokens; an empty list allows only unscoped messages. |
| `scope-case` | `any`, `lower`, or `upper`. |
| `header-max-length` | Positive bounded header length. |
| `description-min-length` | Nonnegative lower bound. |
| `description-max-length` | Optional nonnegative upper bound. |
| `description-ending` | `allow`, `require`, or `forbid` `.`, `!`, and `?` endings. |
| `breaking-markers` | `either` or `paired`; `paired` requires header and footer markers together. |
| `required-footer-tokens` | Up to 128 combined required/forbidden ASCII tokens. |
| `forbidden-footer-tokens` | Exact case-insensitive presence prohibitions. |
| `body-policy` | `optional`, `required`, or `forbidden`. |
| `body-min-length` | Nonnegative prose-body character lower bound. |
| `body-min-words` | Integer from 0 through 100000, counting prose tokens. |
| `body-max-line-length` | Optional maximum body-line length. |
| `dependabot-pull-requests` | `check` (default) or `skip` in event-aware PR checks only. |
| `merge-commits` | `ignore`, `check`, or `reject`, based on Git parent identity. |
| `ignored-headers` | Bounded case-sensitive glob patterns for intentional headers. |
| `max-commits` | Positive bound for a selected commit range. |

`scope-policy-by-type` keys are case-insensitively unique and must be reachable through
`allowed-types`. `required-footer-tokens` and `forbidden-footer-tokens` are case-insensitively
unique, non-overlapping, and cannot use `BREAKING CHANGE` or `BREAKING-CHANGE`.

## Generate and inspect policy

```bash
yaga config init --repo .
yaga config show --repo .
yaga config show --repo . --format json
```

`config init` creates only `.yaga.toml`, refuses to overwrite an existing discovered policy, and
prints the created path and effective values. `config show` is useful for diagnosing discovery,
normalization, and default values before running a check.

## Standalone provider policies

The installed-only providers use separate schema-v1 files so a path policy cannot silently change
a tree or workflow policy:

| File | Required root | Runtime selection |
| --- | --- | --- |
| `.yaga/branch-policy.toml` | `branch-policy-version = 1` | explicit `--name` |
| `.yaga/change-policy.toml` | `change-policy-version = 1` | exact `--range` |
| `.yaga/tree-policy.toml` | `tree-policy-version = 1` | exact `--commit` |
| `.yaga/path-policy.toml` | `path-policy-version = 1` | exact `--commit` |
| `.yaga/mode-policy.toml` | `mode-policy-version = 1` | exact `--commit` |
| `.yaga/size-policy.toml` | `size-policy-version = 1` | exact `--commit` |
| `.yaga/checks/*.toml` | `repository-plan-version` | explicit `--plan` |

These files are quality policy only. A pull-request-controlled plan or policy is never trusted
security authority for a privileged workflow.

## Provider configuration examples

```toml
# branch-policy.toml
branch-policy-version = 1
allowed-patterns = ["main", "feat/*", "fix/*", "dependabot/*/**"]
```

```toml
# change-policy.toml
change-policy-version = 1

[[rules]]
name = "python-source-needs-tests"
when-any = ["src/**/*.py"]
require-any = ["tests/**/*.py"]
```

```toml
# size-policy.toml
size-policy-version = 1
default-blob-limit = 1048576
total-limit = 8388608

[[path-overrides]]
pattern = "docs/**"
blob-limit = 2097152
```

`tree`, `path`, `mode`, and `size` policies operate on committed trees, not the current index or
untracked files. Their exact required keys and closed pattern grammars are documented in
[Repository checks](repository-checks.md).

## Configuration in CI

Keep policy files in the checked-out repository, but keep runtime selectors explicit. A policy file
does not authorize fetching, changing branches, reading secrets, or using a different revision.
For machine consumers, prefer JSON reports over scraping human text.
