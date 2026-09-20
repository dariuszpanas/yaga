# Commit policy

With no input option, YAGA checks `HEAD`. Exactly one explicit source may be selected:

```bash
yaga commit check --message "feat(cli): add configurable checks"
yaga commit check --file .git/COMMIT_EDITMSG
printf 'fix: preserve stdin\n' | yaga commit check --stdin
yaga commit check --commit HEAD~1
yaga commit check --range origin/main..HEAD
yaga commit quality --commit HEAD --offline
```

Optional model advice is covered by the [commit quality guide](commit-quality.md).

Git commit and range sources read complete messages without fetching or invoking a shell. Exact
commit checks select one commit; range checks preserve oldest-first order. Both require complete,
non-shallow history because Git's shallow boundary can hide stored parents. Commit, change, mode,
path, tree, and size selection reject legacy graft overlays, and replacement refs are disabled for
all six.
An empty range, a missing ref, or a selection above the configured commit limit is an operational
error.

All Git-backed checks share one bounded runtime. It resolves an absolute regular Git executable
outside the enclosing worktree and its resolved metadata and object-store boundaries, starts no
shell, inherits only a minimal environment, disables pagers and prompts, contains the spawned
process tree, and enforces hard time and output limits including bounded termination cleanup. Every
command uses Git's global `--no-lazy-fetch` option, so an unsupported client or a missing promised
object fails closed instead of contacting a remote.

The built-in policy enforces only Conventional Commit structure, accepts any type and scope, and
ignores commits that Git proves have multiple parents. Put stricter project policy in the nearest
`.yaga.toml` or `pyproject.toml`. Discovery walks toward the repository root, preferring
`.yaga.toml` in each directory; `--config` selects one file explicitly. Files are never merged, and
unknown or misspelled keys fail loudly.

For `pyproject.toml`:

```toml
[tool.yaga]
config-version = 1

[tool.yaga.commit]
allowed-types = [
  "build", "chore", "ci", "docs", "feat", "fix",
  "perf", "refactor", "revert", "style", "test",
]
type-case = "lower"              # any, lower, or upper
scope-policy = "optional"        # optional, required, or forbidden
scope-policy-by-type = { feat = "required", fix = "required", revert = "forbidden" }
allowed-scopes = ["cli", "config", "git"]
scope-case = "lower"
header-max-length = 100
description-min-length = 3
description-max-length = 72
description-ending = "forbid"    # allow, require, or forbid . ! ?
breaking-markers = "paired"      # either or paired
required-footer-tokens = ["Signed-off-by"]
forbidden-footer-tokens = ["WIP"]
body-policy = "optional"
body-min-length = 0
body-min-words = 8
# Omit body-max-line-length unless this repository wants a wrapping limit.
# Set body-paragraph-splitting = "check" to catch sentence-like prose split by blank lines.
dependabot-pull-requests = "skip" # check or skip in event-aware PR checks
merge-commits = "reject"         # ignore, check, or reject
ignored-headers = ['Revert "*"'] # bounded, case-sensitive glob patterns
max-commits = 64
```

A standalone `.yaga.toml` uses `config-version = 1` and `[commit]` instead of the two
`[tool.yaga...]` tables. Omit `allowed-types` or `allowed-scopes` to allow any value. An explicit
empty `allowed-scopes` list permits only unscoped messages; `allowed-types` must not be empty.
`yaga config show` prints every effective value and the source file, with `--format json` for tools.

`dependabot-pull-requests` defaults to `"check"`. Setting it to `"skip"` skips Conventional Commit
policy evaluation only in `yaga github pull-request check` and the read-only commit-check Action,
and only when the event's pull-request author is exactly the GitHub `dependabot[bot]` Bot account,
following [GitHub's event-author guidance for Dependabot
automation](https://docs.github.com/en/code-security/tutorials/secure-your-dependencies/automate-dependabot-with-actions?learn=dependency_version_updates).
The adapter still validates the complete event and runner context, verifies the checked-out head,
loads configuration, and enumerates the bounded exact commit range before returning a visible
`Dependabot pull request` skip. It does not skip change, mode, path, size, tree, workflow, test, or
review-gate checks.

Ordinary `commit check` and the `commit` provider inside `repo check` never infer Dependabot from a
Git author name, email, message, or branch because those values are author-controlled. They keep
checking normally. A saved event file can reproduce the adapter decision for diagnosis, but its
author identity is only asserted local input; the fixed Action gets host-issued event provenance
from `GITHUB_EVENT_PATH`.

`scope-policy-by-type` defaults to an empty mapping. Each entry replaces the global
`scope-policy` only for that Conventional Commit type, so a project can require scopes for
`feat` and `fix`, keep them optional elsewhere, and forbid them for `revert`. Type matching is
case-insensitive and the same effective policy is used for complete commits and pull-request
titles. `scope-case` and `allowed-scopes` still validate every scope that is present.

The mapping accepts at most 128 safe type tokens and the same closed `optional`, `required`, and
`forbidden` values as the global policy. Configuration rejects case-insensitive duplicate keys and,
when `allowed-types` is configured, override keys outside that list. An empty `allowed-scopes` list
is invalid when any reachable type requires a scope; a non-empty list is invalid when every
reachable type forbids scopes. `header-max-length` validation uses a conservative structural lower
bound so shorter Unicode case-fold equivalents are never rejected during configuration loading;
the commit check remains authoritative for every actual header.

`breaking-markers` defaults to `"either"`. A header `!`, a recognized final
`BREAKING CHANGE:` or `BREAKING-CHANGE:` footer, or both mark a breaking change in that mode. The
`"paired"` policy requires both markers or neither; exactly one produces
`breaking.marker-pair`. Marker pairing applies to complete commit messages, not pull-request titles,
whose policy check intentionally covers only the Conventional Commit header.

`required-footer-tokens` and `forbidden-footer-tokens` default to empty lists. They apply to complete
commit messages, including every commit selected by a range or a pull-request event, but never to
the pull-request title's header-only check. Matching is exact and ASCII case-insensitive: configured
`Signed-off-by` matches `SIGNED-OFF-BY: A User`, but not a token with an added prefix or suffix.
Both `Token: value` and `Token #value` count, provided the value starts with a non-whitespace
character. Repeated tokens are allowed; one occurrence satisfies a requirement, while any
occurrence violates a prohibition. These are presence checks only: requiring `Signed-off-by` does
not validate signer identity, DCO compliance, or a cryptographic signature.

Configured footer tokens contain only one through 128 ASCII letters, digits, or hyphens, and the
two lists may contain at most 128 entries combined. Configuration rejects case-insensitive
duplicates within either list, overlap between the lists, and both reserved breaking-marker token
spellings, `BREAKING CHANGE` and `BREAKING-CHANGE`.

A footer block begins only when a valid footer token starts the first content paragraph or a new
paragraph after a blank line. Once that boundary is found, the rest of the message is footer
content: blank and non-token lines continue a multiline footer value, while a later valid token
starts another footer even without an intervening blank line. Reporting remains bounded:
`footer.required` reports only the first configured missing token at line 1 plus a count of any
other missing tokens, and `footer.forbidden` reports only the earliest forbidden occurrence at its
exact source line.

`body-min-length` and `body-min-words` are independent lower bounds on the parsed prose body.
`body-min-words` accepts an integer from `0` through `100000` and defaults to `0`; it counts
Unicode-whitespace-delimited tokens that contain at least one Unicode alphanumeric character.
Punctuation-only and emoji-only tokens do not count, while text without Unicode whitespace is one
token. The minimum is checked only when a prose body exists, so use `body-policy = "required"` to
require one. A recognized final footer block is not prose body and does not contribute words. When
`body-policy = "forbidden"`, both body minima must be zero.

`body-paragraph-splitting` accepts `skip` (the default) or `check`. The `check` mode catches a
blank-line split between sentence-like one-line paragraphs while leaving standalone short
paragraphs and list items alone. A one-line validation note or justification remains valid; this is
not a minimum paragraph-height rule and does not enforce a wrapping width.

For a new repository, YAGA can create a recommended standalone policy and immediately report its
effective values:

```bash
yaga config init --repo .
```

Use `yaga config init --repo . --dry-run` to validate and inspect the same starter policy without
creating `.yaga.toml`; the report names the path that would be published and marks the preview
explicitly.

The starter permits the common Conventional Commit types, requires a lowercase type, keeps the
scope optional (and lowercase when present), bounds headers, rejects merge commits, and
checks at most 64 commits per range. Initialization creates only `.yaga.toml`: it never edits
`pyproject.toml`, overwrites a path, or shadows a configuration already discovered for the target
directory. Use `--format json` when another tool needs the created path and effective policy.

Diagnostics have stable names such as `syntax.header`, `type.allowed`, `scope.required`,
`scope.forbidden`, `header.length`, `breaking.marker-pair`, `body.word-count`,
`body.paragraph-format`, `footer.required`,
and `footer.forbidden`. Text and versioned JSON reports use these exit codes:

| Exit | Meaning |
| --- | --- |
| `0` | Every checked commit passed; ignored merge/header records may have been skipped. |
| `1` | At least one commit violated policy. |
| `2` | Invocation, input, configuration, or Git failed. |

## Standalone titles

For a standalone title, use `yaga commit check --title "fix: correct the result"`.
This applies header policy and optional spelling, without body or footer requirements.
Titles must be one nonempty line of at most 1,024 UTF-8 bytes. Title input is mutually
exclusive with every other message source.

## Local commit-message hook

For an editor message containing Git comments, use `yaga commit check --edit "$1"` in
a custom `commit-msg` hook. This reads the file without modifying it and delegates
comment removal and whitespace cleanup to `git stripspace --strip-comments`, using
Git's user and repository configuration, including `core.commentChar` and supported
`core.commentString` settings. Comments cannot satisfy body requirements or introduce
spelling findings. This is explicit strip-comments behavior, not an emulation of every
`git commit --cleanup` mode; use literal `--file` when comments are intentional content.
Git command-line `-c` overrides are not inherited. Automatic comment-character selection
is not reconstructed from the editor session; use a fixed configured comment prefix.
Missing files and invalid UTF-8 remain input errors. `--edit` cannot be combined with
another source.

YAGA ships a pre-commit provider for the existing literal file-backed command. Pin the repository to an
audited immutable commit that contains `.pre-commit-hooks.yaml`:

```yaml
repos:
  - repo: https://github.com/dariuszpanas/yaga
    rev: cd02385e3216ac544e7783c7dd6929340e230e95
    hooks:
      - id: yaga-commit-check
```

Install the non-default hook type and its environment:

```bash
pre-commit install --hook-type commit-msg --install-hooks
```

The provider requires pre-commit 3.2 or newer and YAGA requires Python 3.12 or newer. If
pre-commit's default interpreter is older, set `language_version: python3.12` (or another supported
interpreter) on the hook.

Repositories that want ordinary `pre-commit install` to include it can add
`default_install_hook_types: [pre-commit, commit-msg]` to their configuration. Policy remains in
`.yaga.toml` or `pyproject.toml`; do not duplicate it in hook arguments. The hook runs only after
explicit installation, can be bypassed with `--no-verify` or `SKIP=yaga-commit-check`, and does not
run for commits created directly by GitHub, APIs, or bots. File mode has no parent metadata: it
cannot enforce `merge-commits = "reject"` and may apply ordinary syntax rules to a proposed merge
message even when actual merge commits would be ignored. Retain the GitHub commit-policy check as
the repository-side source of truth.

CI should use a non-shallow checkout containing the exact base and head, then call
`yaga commit check --range "$BASE_SHA..$HEAD_SHA"`. `--quiet` suppresses validation reports while
operational errors still go to standard error; `--format json` provides a stable schema for another
tool.

## Optional Typos integration

Set `typos = "check"` to run the installed [Typos CLI](https://github.com/crate-ci/typos) against
each selected commit message. YAGA sends the message through standard input, requests Typos JSON
Lines output, validates bounded finding fields, and converts each finding into a stable
`typos.word` diagnostic. This works with
`--message`, hooks, individual Git commits, ranges, and GitHub pull-request titles and commits.
The default `typos = "skip"` keeps policy results independent of the tools installed on a developer machine.
Set `typos-config = "isolated"` alongside `typos = "check"` to disable Typos configuration
discovery consistently for local titles, complete messages, and hooks. The default
`typos-config = "repository"` preserves discovery. Trusted Action checks always isolate
spelling configuration, regardless of this setting. Local isolation still permits Typos
installed in a project environment; trusted Actions require an executable outside the checkout.
When Typos provides its bounded `byte_offset`, YAGA preserves it as a 1-based diagnostic column;
older or alternate JSON output without that field falls back to column 1.
Text and GitHub output include both the line and column in each policy diagnostic. GitHub
annotations additionally escape the diagnostic for the runner command protocol.

```toml
[commit]
typos = "check"
```

The check is intentionally explicit: when enabled, a missing `typos` executable, malformed output,
unexpected exit status, timeout, or excessive output is an operational error (exit `2`) rather than
an ignored warning. Install Typos separately, for example with `cargo install typos-cli --locked`,
and keep project-specific words in Typos' `_typos.toml`. Use Typos' repository-wide action or
pre-commit integration for source files; YAGA's adapter is narrowly scoped to commit messages.
When `--repo` points at a different checkout, YAGA runs Typos from that repository so its local
Typos configuration is used; direct message and Git-backed sources follow the same rule.

### Spelling in trusted pull-request checks

With the commit Action's `trusted-config` mode, `typos = "check"` applies to both the
PR title and every selected full commit message. Structural title checks remain header-only.
Authentic Dependabot skips and existing skipped commit types also skip spelling checks.
Findings fail with exit `1`; a missing or broken tool fails with exit `2`.

Install a pinned Typos executable outside the checkout before invoking the Action.
Trusted mode passes `--isolated`, using the tool's built-in dictionary without repository,
parent, or global configuration discovery. Custom dictionaries are currently unsupported in
trusted mode: neither a PR's `_typos.toml` nor dirty files in the trusted checkout can weaken it.
Ordinary local checks and the default PR-head mode retain repository Typos configuration.
Keep the tool version consistent when comparing local and CI spelling results.

## Proposed PR messages (development toward 0.2.0)

`pull-request-message = "title-and-body"` opts into full commit policy for the PR title and
description together. The default is `"title-only"`. This setting affects only event-aware
PR checks; see [GitHub Actions](github-actions.md#proposed-merge-messages-development-toward-020)
for merge settings, event triggers, reporting, and limitations.

## Workflow starters and type explanations (development toward 0.2.0)

Start with explicit, editable settings for your workflow:

```bash
yaga config init --starter title-v1 --dry-run
yaga config init --starter complete-message-v1
yaga config show --type fix
yaga config show --type docs --format json
```

`config init` without `--starter` retains the original `recommended-v1` starter byte for byte.
`title-v1` uses lowercase types and a 100-character header limit, allows ending punctuation,
leaves bodies optional, and ignores Git-proven merge commits. It does not disable checks on
commits selected by the caller. `complete-message-v1` uses the same choices and additionally
requires prose for `feat` and `fix`; other types remain optional. Neither starter imposes a
word count or restricts the set of types. Their names are frozen: future recommendations need
new names. Generated TOML is yours to edit, and initialization never replaces existing policy.
`--dry-run` validates and reports the proposed policy without publishing a configuration file.

Both starters retain title-only PR checks. If the PR description is your proposed merge body,
explicitly enable `pull-request-message = "title-and-body"` as described above. Full-message
requirements apply to actual commits and enabled proposed messages, never standalone titles.

`config show --type TYPE` adds the effective body and scope requirements and names the override
or global setting that supplied each. Matching is case-insensitive, just as in the checker.
The JSON report adds `effective_type` only when requested. `listed_type_allowed` tells you whether
the configured type list admits the token; it does not evaluate type casing or prove a complete
message will pass. All independent casing, scope-list, length, footer, and other rules still apply.

The reported `config_path` identifies the single selected file, or built-in defaults. A project
configuration replaces the global fallback as a whole; fields are not merged between files.
For trusted Actions, inspect the committed default-branch policy used by that runner: a local
`config show` does not reproduce a trusted Action's revision selection automatically.
