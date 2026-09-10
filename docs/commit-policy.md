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
scope-case = "lower"
header-max-length = 100
description-min-length = 3
description-ending = "forbid"
breaking-markers = "paired"
body-policy = "optional"
body-min-words = 8
body-max-line-length = 100
dependabot-pull-requests = "skip"
merge-commits = "reject"
max-commits = 64
```

YAGA loads exactly one nearest `.yaga.toml` or `pyproject.toml`; files are never merged. An
explicit `--config` selects one source. Unknown keys, invalid types, duplicate normalized tokens,
and unsupported schema versions fail closed.

`scope-policy-by-type` overrides the global scope policy for complete commits and PR titles.
`scope-case` and `allowed-scopes` remain independent checks. `breaking-markers` accepts `either`
or `paired`; pairing requires both a header `!` and a recognized final breaking footer, or neither.

## Bodies and footers

`body-min-words` counts Unicode-whitespace-delimited prose tokens that contain a Unicode
alphanumeric character. Recognized final footers are excluded. A nonzero minimum does not make an
optional absent body required, and body minima must be zero when the body is forbidden.

Required and forbidden footer tokens apply to complete commit messages, never pull-request titles.
Matching is case-insensitive and recognizes both `Token: value` and `Token #value`; these are
presence checks, not signer, DCO, or signature validation. Breaking-marker spellings are reserved.

## Git selection and hooks

```bash
yaga commit check --message "feat(cli): add a check"
yaga commit check --commit HEAD~1
yaga commit check --range origin/main..HEAD
```

Git selection is shell-free, disables replacement refs and lazy fetching, rejects unsafe revisions,
and bounds messages, ranges, output, and diagnostics. Merge-commit behavior is controlled by
`merge-commits`; it is not inferred from the commit subject.

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
