# Configuration

## Opt-in consumer commit constraints

The following schema-v1 options leave existing defaults unchanged:

| Option | Default | Meaning |
| --- | --- | --- |
| `description-case` | `"any"` | `"forbid-initial-uppercase"` rejects an initial Unicode uppercase letter; quoted, numeric, uncased and titlecase initials remain allowed. |
| `length-unit` | `"codepoints"` | `"utf16"` counts UTF-16 code units for header, description, body and line-length bounds, matching JavaScript string lengths. Byte and word limits are unchanged. |
| `footer-syntax` | `"conventional"` | `"colon-whitespace"` permits one or more whitespace characters after a footer colon, including a tab. The default retains the exact colon-space/hash-space grammar. |
| `description-ending` | `"allow"` | `"forbid-period"` rejects only a final period, preserving question and exclamation endings. Existing `allow`, `require`, and `forbid` retain their meanings. |
| `line-length-urls` | `"check"` | `"exempt"` exempts entire body/footer lines containing lowercase `http://` or `https://` followed by non-whitespace, with no preceding ASCII word character. It does not validate URLs. |
| `footer-max-line-length` | unset | Positive character limit through 100,000, including continuation lines; reports `footer.line-length`. |
| `required-colon-footer-tokens` | `[]` | Case-sensitive, nonempty colon trailers; `Validation #123` and `validation: checked` do not satisfy `Validation`. Reports `footer.required-colon`. |

Required colon tokens use the existing bounded ASCII token grammar and the combined 128-entry
footer-policy budget. They may not overlap forbidden tokens. Use `footer-syntax = "colon-whitespace"`
to accept tabs or multiple spaces after colons; the footer must still begin at a paragraph
boundary. Existing `required-footer-tokens` retains its
case-insensitive colon/hash semantics. These options do not establish identity or attest that
the described validation actually occurred. Footer and body rules never apply to PR titles.

These settings are individual policy controls, not a general commitlint compatibility mode.
YAGA retains its own structural parser; qualify the complete consumer corpus before migration.

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

### Global defaults and project isolation

For a global installation (uv tool, pipx, or system Python), a user configuration is a fallback
only when no project configuration exists. Its location is `%APPDATA%/yaga/config.toml` on
Windows and `$XDG_CONFIG_HOME/yaga/config.toml` (default `~/.config/yaga/config.toml`) elsewhere.
It uses the standalone `.yaga.toml` shape. Create this file explicitly; installation does not
create policy for you.

The precedence is **explicit `--config`, nearest project file, global file, built-in defaults**.
One file supplies the entire configuration: a project file never inherits omitted keys from global
settings. An invalid project file fails instead of falling back to the global file.
`yaga config show --repo .` displays the effective source and policy.

Ordinary virtual environments, including project `.venv` installations, ignore global settings.
uv and pipx tool environments are identified by their manager receipt in the environment root.
Global fallback is also disabled when `CI`, `GITHUB_ACTIONS`, or either YAGA Action runtime
indicator is nonempty. `config init` always creates project policy independently of user defaults.

### Required YAGA version

```toml
[tool.yaga]
required-version = ">=0.1.0, <1.0.0"
```

In a standalone or global configuration, omit the `[tool.yaga]` header. This optional root key
checks the running YAGA release offline when configuration is loaded. A mismatch is a configuration
error (exit 2), before commit policy evaluation; it never downloads or updates software. Project
requirements replace global requirements along with the rest of the file.

Requirements accept `>=`, `>`, `<=`, `<`, `==`, and `!=` against final `MAJOR.MINOR.PATCH`
versions, joined by commas (all must match). Each numeric component has at most nine digits and
no leading zeroes. The limit is eight comparisons and 256 characters. Wildcards, compatible-release
operators, prereleases, URLs, and other Python packaging requirement syntax are not supported.

This setting applies to commands that load this configuration: commit checks and quality, the
GitHub commit adapter, repository checks selecting the commit provider, and `config show`.
Explicit-policy providers such as `branch check` and `tree check` retain their no-discovery
contract. Use `yaga config show --config pyproject.toml` as an explicit version preflight before
those checks. Help, `--version`, and `self update` remain available to recover from a mismatch.

See [installation and updates](getting-started.md#update-or-uninstall) for changing the installed
version. Pin the installation separately when CI must always use one exact release.

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
| `body-policy-by-type` | Empty-default mapping of up to 128 types to body presence overrides. |
| `body-min-length` | Nonnegative prose-body character lower bound. |
| `body-max-length` | Optional total prose-body limit from 1 through 100000; independent of line wrapping. |
| `body-min-words` | Integer from 0 through 100000, counting prose tokens. |
| `body-max-line-length` | Optional maximum body-line length; omitted means unlimited. |
| `body-paragraph-splitting` | `skip` (default) or `check` for likely sentence splits across blank lines. |
| `skip-pull-request-authors` | Empty-default list of up to 128 explicit GitHub PR author logins exempt from title and commit checks. |
| `dependabot-pull-requests` | `check` (default) or `skip` in event-aware PR checks only. |
| `typos` | `skip` (default) or `check` with an installed Typos CLI. |
| `typos-config` | `repository` (default) discovers Typos settings; `isolated` disables discovery. Trusted Actions always isolate. |
| `quality` | Nested non-secret defaults for `commit quality`; provider, task, model, revision, threshold, region, max-tokens, max-input-tokens, and input-mode. |
| `merge-commits` | `ignore`, `check`, or `reject`, based on Git parent identity. |
| `ignored-headers` | Bounded case-sensitive glob patterns for intentional headers. |
| `max-commits` | Positive bound for a selected commit range. |

`scope-policy-by-type` keys are case-insensitively unique and must be reachable through
`allowed-types`. `required-footer-tokens` and `forbidden-footer-tokens` are case-insensitively
unique, non-overlapping, and cannot use `BREAKING CHANGE` or `BREAKING-CHANGE`.

### Body layout settings

Development toward 0.2.0 adds `body-policy-by-type` and `body-max-length`; these options are not
available in 0.1.2. A project can require an explanation for behavioral changes while keeping
documentation and maintenance bodies optional:

```toml
[commit]
body-policy = "optional"
body-min-length = 20
body-max-length = 2000
body-policy-by-type = { feat = "required", fix = "required" }
```

Each case-insensitive type override replaces only body presence, not the other checks. Values
are `optional`, `required`, or `forbidden`; duplicate normalized keys and keys outside configured
`allowed-types` are errors. Length and word minima apply when an allowed body exists; they do
not require an optional body and do not create extra minimum findings for a forbidden body.
All reachable body policies cannot be forbidden alongside a positive minimum.
The total limit counts stripped prose, including internal newlines, in the configured
`length-unit`; recognized final footers are excluded. It cannot be smaller than `body-min-length`
or too small to contain `body-min-words` one-character words separated by whitespace.
Both limits report `body.length`. No body requirement or limit applies to a standalone title.

`body-max-line-length` is an optional wrapping limit; omitting it leaves body line length
unlimited. `body-paragraph-splitting` is an explicit layout heuristic. `skip` (the default) allows
any paragraph layout. `check` identifies the first blank-line boundary where a one-line prose
paragraph appears to continue into the next one-line prose paragraph. List items beginning with
`-`, `*`, `+`, or a numbered marker such as `1.` are ignored. A one-line paragraph is not an error
by itself: this setting does not require every prose paragraph to wrap onto two physical lines.

The check is continuation-aware: it reports a boundary when the previous paragraph ends in
continuation punctuation such as `,`, `;`, `:`, or a dash, or when the next paragraph starts with a
lowercase letter. A paragraph that simply ends without punctuation remains valid. For example, this
is valid with `body-paragraph-splitting = "check"` because the paragraphs are complete notes:

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

Quality reports count the complete selected message and its body lines, but those counts describe
the input selection, not the model's token window. YAGA bounds the provider input to 12,000
characters; Hugging Face classification and seq2seq adapters additionally tokenize with a 512-token
limit, so a long message may be truncated at the model boundary. Keep exact structural and lexical
requirements in the normal commit policy, which receives the complete bounded message, and use the
quality result as an advisory signal.

```toml
[tool.yaga.commit.quality]
provider = "huggingface"
task = "classification"
model = "saridormi/commit-message-quality-codebert"
revision = "30c7895b3eb0270a3246ef3db7b43c837d8e553a"
threshold = 0.70
max-tokens = 32
max-input-tokens = 512
input-mode = "message"
```

`provider` is `huggingface` or `bedrock`; `task` is `classification` or `seq2seq`. The Bedrock
provider currently supports `classification` only; `seq2seq` is a Hugging Face mode. Hugging Face
requires a lowercase hexadecimal revision. Bedrock does not use `revision` and can set `region`
and a provider-specific `model`. `max-tokens` is bounded from 1 through 256, while
`threshold` is inclusive from 0 through 1; zero intentionally flags every nonnegative classifier
score. `max-input-tokens` defaults to 512 and is bounded from 1 through 4096 for Hugging Face models.
The CLI `--max-input-tokens` option overrides it for one invocation. Use `--config` when the quality
settings should come from one explicit file rather than normal nearest-file discovery.

`input-mode` controls the text sent to the provider. It accepts `message` (the default, sending the
complete commit message including its body) or `title` (sending only the first line). Reports keep
the selected message bounded for display, identify the mode, and include exact model-input
character coverage so a title-only result is not mistaken for a full-message check. The CLI
`--input-mode` option overrides this default for one invocation.

The development quality extra uses PyTorch's CPU wheel index on Linux because YAGA's quality
adapters run CPU inference; this avoids installing CUDA libraries in the hosted quality workflow.
Windows and macOS retain their platform-specific PyTorch wheels. The build smoke test honors this
declared, locked source while installing only the base runtime; the model cache is separate from the
Python package cache, so changing or clearing one does not silently make the other complete.

## Generate and inspect policy

```bash
yaga config init --repo .
yaga config init --repo . --dry-run
yaga config show --repo .
yaga config show --repo . --format json
```

`config init` creates only `.yaga.toml`, refuses to overwrite an existing discovered policy, and
prints the created path and effective values. `config show` is useful for diagnosing discovery,
normalization, and default values before running a check. Add `--dry-run` to perform the same
directory, discovery, target, and policy validation without publishing the file; its report names
the `.yaga.toml` path that would be created and marks the result as `dry_run` in JSON (or
`mode: dry-run (no file created)` in text).

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

## Explicit PR author exemptions

For dependency update services or other explicitly exempt accounts:

```toml
[tool.yaga.commit]
skip-pull-request-authors = ["renovate[bot]", "release-service"]
```

Use `[commit]` in `.yaga.toml`. This list is empty by default. Logins match the validated
PR author case-insensitively and must be literal names, not patterns, emails, Git authors,
committers, triggering actors, or branch names. Each entry is at most 128 ASCII characters
(letters, digits, underscore, dot, hyphen, and an optional trailing `[bot]`); duplicate
case-insensitive entries are rejected. Both Bot and User accounts can be listed explicitly.

The exemption skips title and full-message structural and spelling checks only after event,
checkout, policy, and commit-range validation. Reports use `Configured pull request author`.
It does not exempt ordinary `commit check`, repository checks, or other providers.
Unlike the narrower `dependabot-pull-requests = "skip"` option in trusted mode, this explicit
list applies regardless of head repository or branch. Listing an account opts into that scope.
The existing Dependabot option retains its identity and trusted same-repository/branch checks.

Use `trusted-config` when the exemption must come from the default-branch policy. Default
PR-head mode intentionally reads PR policy, including this list; it is not trusted merge authority.
