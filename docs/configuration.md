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
| `body-max-line-length` | Optional maximum body-line length; omitted means unlimited. |
| `body-max-consecutive-single-line-paragraphs` | Optional maximum run length of one-line prose paragraphs; omitted means unlimited. |
| `dependabot-pull-requests` | `check` (default) or `skip` in event-aware PR checks only. |
| `typos` | `skip` (default) or `check` with an installed Typos CLI. |
| `quality` | Nested non-secret defaults for `commit quality`; provider, task, model, revision, threshold, region, and max-tokens. |
| `merge-commits` | `ignore`, `check`, or `reject`, based on Git parent identity. |
| `ignored-headers` | Bounded case-sensitive glob patterns for intentional headers. |
| `max-commits` | Positive bound for a selected commit range. |

`scope-policy-by-type` keys are case-insensitively unique and must be reachable through
`allowed-types`. `required-footer-tokens` and `forbidden-footer-tokens` are case-insensitively
unique, non-overlapping, and cannot use `BREAKING CHANGE` or `BREAKING-CHANGE`.

### Body layout settings

`body-max-line-length` is an optional wrapping limit; omitting it leaves body line length
unlimited. `body-max-consecutive-single-line-paragraphs` is a separate optional layout limit. It
finds the longest run of prose paragraphs made of exactly one non-empty line after parsing the
header and final footer block. List items beginning with `-`, `*`, `+`, or a numbered marker such as
`1.` are not counted, and a wrapped paragraph breaks the run. Omit the setting to allow any layout,
set it to `0` for the same unlimited behavior, or set it to `1` to permit standalone one-line
paragraphs while flagging a likely sentence split by blank lines. A one-line paragraph is not an
error by itself.

The check is continuation-aware: it only extends a run when the previous line does not end in `.`,
`!`, or `?`, or when the next paragraph starts with a lowercase letter. For example, this is valid
with a limit of `1` because the paragraphs are complete notes:

```text
The parser accepts the legacy form.

Validation: the compatibility test still passes.
```

Whereas this is reported as `body.paragraph-format` because the blank lines interrupt one sentence:

```text
The parser accepts the legacy form,

including input received from older clients

when compatibility mode is enabled.
```

The check reports `body.paragraph-format` at the first offending paragraph and explains that the
run is likely a sentence split across blank lines. It is deterministic and does not depend on the
optional quality model. This makes it suitable for enforcing repository formatting policy when a
model would correctly recognize the words but overlook their blank-line layout.

### Quality advisory settings

The optional model advisory reads `[tool.yaga.commit.quality]` in `pyproject.toml`, or
`[commit.quality]` in `.yaga.toml`. These settings are defaults for `yaga commit quality`; every
corresponding CLI option overrides them for one invocation. Credentials are never valid here.

```toml
[tool.yaga.commit.quality]
provider = "huggingface"
task = "classification"
model = "saridormi/commit-message-quality-codebert"
revision = "30c7895b3eb0270a3246ef3db7b43c837d8e553a"
threshold = 0.70
max-tokens = 32
```

`provider` is `huggingface` or `bedrock`; `task` is `classification` or `seq2seq`. Hugging Face
requires a lowercase hexadecimal revision. Bedrock may omit `revision` and can set `region` and a
provider-specific `model`. `max-tokens` is bounded from 1 through 256. Use `--config` when the
quality settings should come from one explicit file rather than normal nearest-file discovery.

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
| `.yaga/tree-policy.toml` | `tree-policy-version = 1` | exact `--revision` |
| `.yaga/path-policy.toml` | `path-policy-version = 1` | exact `--revision` |
| `.yaga/mode-policy.toml` | `mode-policy-version = 1` | exact `--revision` |
| `.yaga/size-policy.toml` | `size-policy-version = 1` | exact `--revision` |
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
default-max-blob-bytes = 1048576
max-total-blob-bytes = 8388608

[[path-limits]]
pattern = "docs/**"
max-blob-bytes = 2097152
```

```toml
# mode-policy.toml
mode-policy-version = 1
default-allowed-modes = ["regular"]

[[path-overrides]]
pattern = "scripts/**"
allowed-modes = ["executable"]
```

```toml
# path-policy.toml: use the frozen portability profile
path-policy-version = 1
profile = "windows-compatible-v1"
```

```toml
# tree-policy.toml
tree-policy-version = 1
required-paths = ["README.md", "pyproject.toml"]
forbidden-patterns = ["**/.env", "dist/**"]
```

`tree`, `path`, `mode`, and `size` policies operate on committed trees, not the current index or
untracked files. Their policy files are explicit runtime inputs: they are not provenance claims
about the selected revision. See [Repository checks](repository-checks.md) for the exact limits,
diagnostics, and revision requirements.

## Choosing policy boundaries

Use the narrowest provider that expresses the requirement. `commit` checks message structure and
footers; `change` checks relationships between changed paths; `branch` checks one supplied branch
name; `tree`, `path`, `mode`, and `size` check one committed snapshot; and the workflow providers
check Actions files. Combine them only through explicit `repo check` selections or a checked-in
plan. A policy file never causes another provider to run.

For a committed-tree provider, always pass the exact revision that CI checked out:

```bash
yaga size check --repo . --policy .yaga/size-policy.toml --revision "$GITHUB_SHA"
```

The CLI does not fetch, infer a revision from the working tree, or inspect staged and untracked
files. A shallow checkout is valid only when Git already has the selected commit, tree, and (for
`size`) the required blob objects.

## Configuration in CI

Keep policy files in the checked-out repository, but keep runtime selectors explicit. A policy file
does not authorize fetching, changing branches, reading secrets, or using a different revision.
For machine consumers, prefer JSON reports over scraping human text.
