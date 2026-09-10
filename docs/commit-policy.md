# Commit policy

`commit check` is YAGA's first general-purpose provider. It validates Conventional Commit
structure while keeping parser behavior separate from the configured policy.

## Configuration

Configuration is schema version 1 in either `[tool.yaga]` and `[tool.yaga.commit]` in
`pyproject.toml`, or `[commit]` in a standalone `.yaga.toml`:

```toml
[tool.yaga]
config-version = 1

[tool.yaga.commit]
allowed-types = ["build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test"]
type-case = "lower"
scope-policy = "optional"
scope-policy-by-type = { feat = "required", fix = "required" }
allowed-scopes = ["api", "cli", "config"]
scope-case = "lower"
header-max-length = 100
description-min-length = 3
description-max-length = 72
description-ending = "forbid"
breaking-markers = "paired"
required-footer-tokens = ["Refs"]
forbidden-footer-tokens = ["WIP"]
body-policy = "optional"
body-min-words = 8
body-max-line-length = 100
dependabot-pull-requests = "skip"
typos = "skip"
merge-commits = "reject"
max-commits = 64
```

YAGA loads exactly one nearest `.yaga.toml` or `pyproject.toml`; files are never merged. An
explicit `--config` selects one source. Unknown keys, invalid types, duplicate normalized tokens,
and unsupported schema versions fail closed.

`scope-policy-by-type` overrides the global scope policy for complete commits and PR titles.
The override replaces only scope presence; `scope-case` and `allowed-scopes` still validate every
scope that is present. Matching is case-insensitive for the type key, while configured type and
scope case policies remain independent. An empty `allowed-scopes` list permits only unscoped
messages, and `allowed-types` must not be empty when supplied.

`description-min-length` and `description-max-length` apply to the description after the type and
optional scope. `description-ending` controls only `.`, `!`, and `?`; it does not rewrite or strip
the message. `breaking-markers` accepts `either` or `paired`; pairing requires both a header `!`
and a recognized final breaking footer, or neither.

## Bodies and footers

`body-min-words` counts Unicode-whitespace-delimited prose tokens that contain a Unicode
alphanumeric character. Recognized final footers are excluded. A nonzero minimum does not make an
optional absent body required, and body minima must be zero when the body is forbidden.

Required and forbidden footer tokens apply to complete commit messages, never pull-request titles.
Matching is exact and case-insensitive and recognizes both `Token: value` and `Token #value`;
repeated tokens are allowed. These are presence checks, not signer, DCO, or signature validation.
Tokens use one to 128 ASCII letters, digits, or hyphens; the two lists share a 128-entry bound,
must not overlap, and cannot use the reserved breaking-marker spellings.

Footer parsing starts only when a valid token begins the first content paragraph or a later
paragraph after a blank line. Once the boundary is found, blank and non-token lines remain part of
the current multiline footer value until a later valid token begins another footer. The stable
`footer.required` and `footer.forbidden` diagnostics retain source-line attribution while bounding
displayed details.

`body-min-length` and `body-min-words` are independent lower bounds. Word counting splits on
Unicode whitespace and counts only tokens containing a Unicode alphanumeric character; punctuation
and emoji-only tokens do not count, and recognized footer content is excluded. A nonzero minimum
does not make an optional body required. Use `body-policy = "required"` when presence matters, and
keep both minima at zero when `body-policy = "forbidden"`.

YAGA’s own repository policy sets `body-policy = "required"` with a minimum prose length, so normal
human commits include a durable explanation. The event-aware commit Action still skips policy
evaluation for the exact Dependabot bot identity when `dependabot-pull-requests = "skip"`, including
multiline Dependabot commit messages.

## Optional Typos integration

Set `typos = "check"` to run the installed [Typos CLI](https://github.com/crate-ci/typos) against
each selected commit message. YAGA sends the message through standard input, requests Typos JSON
Lines output, and converts each finding into a stable `typos.word` diagnostic. This works with
`--message`, hooks, individual Git commits, and ranges; the default `typos = "skip"` keeps policy
results independent of the tools installed on a developer machine.

```toml
[commit]
typos = "check"
```

The check is intentionally explicit: when enabled, a missing `typos` executable, malformed output,
unexpected exit status, timeout, or excessive output is an operational error (exit `2`) rather than
an ignored warning. Install Typos separately, for example with `cargo install typos-cli --locked`,
and keep project-specific words in Typos' `_typos.toml`. Use Typos' repository-wide action or
pre-commit integration for source files; YAGA's adapter is narrowly scoped to commit messages.

## Git selection and hooks

```bash
yaga commit check --message "feat(cli): add a check"
yaga commit check --commit HEAD~1
yaga commit check --range origin/main..HEAD
```

Git selection is shell-free, disables replacement refs and lazy fetching, rejects unsafe revisions,
and bounds messages, ranges, output, and diagnostics. Merge-commit behavior is controlled by
`merge-commits`; it is not inferred from the commit subject.

The source modes are deliberately exclusive:

| Source | Checks | Typical use |
| --- | --- | --- |
| `--message` | One supplied message | Editor or test feedback. |
| `--file` | One UTF-8 file | The `commit-msg` hook. |
| `--stdin` | One UTF-8 stream | Shell or editor integration. |
| `--commit` | One Git commit | Inspecting an existing commit. |
| `--range` | Every commit, oldest first | Pull-request or release validation. |

With no source, YAGA checks `HEAD`. It never combines sources or silently changes a missing base.
Git-backed checks require the relevant history and do not fetch it.

YAGA also exposes one direct `commit-msg` pre-commit adapter. Install it explicitly with
`pre-commit install --hook-type commit-msg --install-hooks`. The hook cannot inspect parent
metadata, so the repository-side range check remains authoritative.

## Dependabot and reports

`dependabot-pull-requests = "skip"` applies only to the event-aware GitHub PR command and the
read-only commit Action. It requires the exact `dependabot[bot]` login, `Bot` type, and bounded
positive ID from the parsed PR author object. Ordinary commit and repository checks continue to
run.

Stable diagnostics include `syntax.header`, `type.allowed`, `scope.required`, `header.length`,
`breaking.marker-pair`, `body.word-count`, `footer.required`, and `footer.forbidden`. Use
`--format json` for the versioned machine contract or `--format github` for escaped annotations.

Complete commit messages receive body, footer, and merge-parent rules. Pull-request titles are
checked only as a Conventional Commit header: body minima, footer tokens, breaking-footer pairing,
and merge-parent policy do not apply to the title. The event-aware adapter then checks the exact
event `base.sha..head.sha` commit range separately.
