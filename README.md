# YAGA

YAGA is an extensible Python CLI for repository policy that runs the same checks locally and in CI.
Its first general-purpose feature is a configurable
[Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) checker. The Agent
review policy can describe one or more configured review lenses. Its experimental GitHub Action is
currently a single-lens Codex adapter; multi-lens execution uses the provider-neutral plan and
receipt commands through an external adapter.

YAGA is pre-release. The CLI and configuration schema may still change. Action consumers must pin
the exact audited 40-character commit SHA they canaried rather than a branch or mutable tag.

## Install and explore

Python 3.12 or newer is required. From a checkout:

```bash
uv sync --group dev
uv run yaga --help
```

The full operator and security reference is available in the
[Zensical documentation](https://dariuszpanas.github.io/yaga/). The source lives in `docs/` and
can be previewed with `make docs-serve`.

The distribution is named `yaga-cli`; the executable and Python package are both named `yaga`.
The `yaga` distribution name on PyPI belongs to an unrelated project.

The command tree uses deliberately separated groups:

```text
yaga branch check                  # validate one explicit short branch name
yaga change check                  # enforce changed-path coupling for one Git range
yaga commit check                  # validate one message, commit, or range
yaga commit quality                # optional provider-backed quality advisory
yaga config init                   # create a safe standalone starter policy
yaga config show                   # explain the effective policy and its source
yaga github pull-request check     # validate one exact GitHub PR event
yaga mode check                    # enforce committed entry modes
yaga path check                    # validate committed path portability
yaga repo check --check commit     # aggregate an explicit provider set
yaga size check                    # enforce blob and total bytes for one committed tree
yaga tree check                    # validate one exact committed tree
yaga workflow check                # require immutable workflow references
yaga workflow security             # enforce a bounded Actions trust policy
yaga workflow lint                 # lint workflow syntax with pinned actionlint
yaga gate agent-review <operation> # run the configured review gate
yaga gate agent-review policy check # validate a named-lens policy
yaga gate agent-review policy plan  # inspect provider-neutral lens work
yaga gate agent-review policy template # scaffold a complete pending receipt
yaga gate agent-review policy evaluate # aggregate an adapter receipt
```

## Conventional Commit checks

With no input option, YAGA checks `HEAD`. Exactly one explicit source may be selected:

```bash
yaga commit check --message "feat(cli): add configurable checks"
yaga commit check --file .git/COMMIT_EDITMSG
printf 'fix: preserve stdin\n' | yaga commit check --stdin
yaga commit check --commit HEAD~1
yaga commit check --range origin/main..HEAD
yaga commit quality --commit HEAD --offline
```

`commit quality` also reads non-secret defaults from `[tool.yaga.commit.quality]` in
`pyproject.toml` (or `[commit.quality]` in `.yaga.toml`); use `--config` for an explicit file and
override individual settings with CLI flags. Provider credentials are always supplied through the
provider's normal environment/SDK credential chain, never through YAGA configuration.
The default Hugging Face classifier is an optional advisory for message quality; use
`uv sync --extra quality-bedrock` and `--provider bedrock` when inference should run through AWS
Bedrock instead. Quality findings do not replace deterministic `commit check` rules, which remain
responsible for structure, scope, body, footer, paragraph, and spelling policy.
Use `--max-input-tokens` or `max-input-tokens` in the quality table to tune the Hugging Face
tokenizer window; it defaults to 512 and accepts values through 4096. Reports identify the
effective bound alongside the selected message and body-line counts. Hugging Face reports also show
the measured input-token count and state whether each message was actually truncated at the model
window. Quality sends the complete message, including the body, by default; use `--input-mode title`
or `input-mode = "title"` when title-only scoring is intentional. Reports identify the selected
mode. Use `--format github` in Actions for escaped warning annotations when the advisory flags a
message; provider failures remain error annotations with exit `2`.

See the [commit policy guide](docs/commit-policy.md#optional-model-quality-advisory) for source
modes, provider/task compatibility, cache and offline replay, configuration precedence, and report
interpretation.

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
creating `.yaga.toml`; the report names the path that would be published.

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

### Local commit-message hook

YAGA ships a pre-commit provider for the existing file-backed command. Pin the repository to an
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

## Branch-name checks

`yaga branch check` validates one explicit short branch name against one explicit versioned policy.
It does not inspect the current checkout, invoke Git, discover configuration, read an event payload,
or normalize remote and full-ref prefixes. That makes the same command deterministic in a local
hook, a CI workflow, or a built-wheel smoke test:

```toml
# .yaga/branch-policy.toml
branch-policy-version = 1
allowed-patterns = [
  "main",
  "feat/*",
  "fix/*",
  "dependabot/*/**",
]
```

```bash
yaga branch check \
  --policy .yaga/branch-policy.toml \
  --name feat/branch-policy
```

Schema version 1 accepts one through 64 unique, case-sensitive patterns. A component-local `*`
matches within one slash-delimited component, while a whole `**` component matches zero or more
components. Matching is anchored to the complete name. Patterns and names use a bounded portable
ASCII grammar that rejects whitespace, shell punctuation, unsafe component edges, `..`, and
`.lock` endings. Names additionally reject whole-name `HEAD`, case-insensitive `refs/` prefixes,
and exact 40- or 64-character hexadecimal lookalikes. The 244-byte name limit is YAGA's portable
policy boundary, not a claim about every Git host.

A syntactically invalid name reports `branch.syntax`; a valid name not admitted by the policy
reports `branch.allowed`. Text, versioned JSON, and escaped GitHub reports return exit `0` for a
match, exit `1` for either finding, and exit `2` for invocation, input, or policy errors. The JSON
report includes a bounded terminal-safe rendering of the selected name, the ordered allowed
patterns, and the first matching pattern.

For Actions, copy the untrusted context value into an environment variable and quote it as one
argument. Pull requests use `github.head_ref`; branch pushes use `github.ref_name`. Do not pass a
pull request's synthetic `<number>/merge` ref name or infer a branch from checkout state:

```yaml
- name: Check pull-request branch name
  if: github.event_name == 'pull_request'
  env:
    YAGA_BRANCH_NAME: ${{ github.head_ref }}
  run: >-
    uv run yaga branch check
    --policy .yaga/branch-policy.toml
    --name "$YAGA_BRANCH_NAME"
    --format github
```

## Changed-path coupling checks

`yaga change check` enforces small repository relationships such as “Python source changes require
at least one test change.” Policy and Git range are both explicit; YAGA never guesses from the
working tree, discovers a policy, fetches history, or calls GitHub.

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
  --range origin/main...HEAD
```

Version 1 supports only `when-any` plus `require-any`. A rule is skipped when no changed path
matches `when-any`, passes when triggered and at least one changed path matches `require-any`, and
otherwise reports `change.require_any`. Rule names are bounded lowercase slugs. Patterns are
case-sensitive, repository-relative POSIX paths with literal characters, component-local `*`, and
whole-component `**`; there are no exclusions, regexes, includes, expressions, environment
interpolation, or commands. Literal `$` and `%` stay literal.

Unique patterns are compiled and cached once per check. A shared hard budget of 10,000,000 matcher
work units bounds the combined pattern/path evaluation; exceeding it is an operational error with
exit `2`, so individually valid but excessively complex policies and diffs still fail closed.

YAGA accepts exactly one complete-history `A..B` or `A...B` range. Two dots compare the exact
endpoint trees. Three dots compare the unique merge base with the right endpoint, which matches the
usual pull-request view; ambiguous merge bases fail closed. Changed paths come from a bounded,
NUL-delimited, rename-disabled Git diff, so a rename is evaluated as a deletion plus an addition.
All change kinds count as matching paths in schema v1, including additions, modifications,
deletions, and gitlink updates; the policy deliberately does not infer file status or content.
Missing objects, shallow history, legacy graft overlays, invalid UTF-8 paths, unsafe paths, or an
over-budget diff are operational errors.

Text, versioned JSON, and escaped GitHub reports use exit `0` when every triggered rule passes,
exit `1` for coupling violations, and exit `2` for policy, range, repository, or Git failures. In an
ordinary unprivileged pull-request workflow, fetch complete history without persisting checkout
credentials and pass event SHAs through environment variables rather than interpolating them into
shell source:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  with:
    ref: ${{ github.event.pull_request.head.sha }}
    fetch-depth: 0
    persist-credentials: false
- name: Check changed-path coupling
  env:
    YAGA_CHANGE_RANGE: ${{ format('{0}...{1}', github.event.pull_request.base.sha, github.event.pull_request.head.sha) }}
  run: >-
    uv run yaga change check
    --policy .yaga/change-policy.toml
    --range "$YAGA_CHANGE_RANGE"
    --format github
```

This is an installed CLI check, not a privileged Action or merge-security authority. The caller is
responsible for installing its pinned YAGA environment and must not run pull-request-controlled
code from `pull_request_target` or `workflow_run` with trusted credentials.

## Committed-tree checks

`yaga tree check` validates the tracked leaf paths in one explicit commit against one explicit
versioned policy. It is a snapshot invariant check: `change check` answers whether one delta has
the required companion changes, while `tree check` answers whether the resulting committed tree
contains required project files and excludes known local or generated artifacts.

```toml
# .yaga/tree-policy.toml
tree-policy-version = 1
required-paths = [
  ".yaga/tree-policy.toml",
  "LICENSE",
  "README.md",
  "SECURITY.md",
  "pyproject.toml",
  "uv.lock",
]
forbidden-patterns = [
  "**/.env",
  "**/__pycache__/**",
  "**/.venv/**",
  "**/*.pyc",
  "build/**",
  "dist/**",
]
```

```bash
yaga tree check \
  --policy .yaga/tree-policy.toml \
  --revision HEAD
```

Both arrays are required in schema version 1 and either may be empty, but the policy must contain
one through 128 entries in total. Required paths are exact. Forbidden patterns are anchored and
case-sensitive, with component-local `*` and whole-component `**`; a required path that is also
forbidden makes the policy invalid. Paths are canonical repository-relative UTF-8 POSIX names,
bounded to 4,096 bytes and 64 components. Selected trees are bounded to 50,000 leaf paths and
64 MiB of Git output. Pattern evaluation shares a hard 10,000,000-work-unit ceiling.

YAGA resolves the selected value as exactly one commit and enumerates its tree with bounded,
NUL-delimited Git output. It never fetches, reads tracked file contents, follows a submodule, or
consults the worktree, index, or untracked files. Regular and executable files, symlinks, and
gitlinks all count as leaf paths. Legacy graft overlays are rejected before revision resolution and
rechecked after enumeration. A shallow checkout is acceptable when the selected commit and tree
objects already exist. The runtime checks the outermost enclosing repository even when
`--repo` names a worktree subdirectory, plus bounded resolved `.git`, linked-worktree `commondir`,
object-directory, and local alternate-object-store metadata. Alternate chains are cycle-safe and
limited to 128 directories. Repository pointer and alternate metadata must be bounded regular files
that retain their identity while opened; special files fail closed before Git starts.
This is path policy, not secret-content detection.

Missing paths report `tree.required`; tracked paths matching the first configured forbidden
pattern report `tree.forbidden`. Text, versioned JSON, and escaped GitHub reports return exit `0`
for a clean tree, exit `1` for findings, and exit `2` for policy, revision, repository, Git, or
resource-limit errors. Reports bound displayed diagnostics while retaining exact checked-entry and
finding counts.

The policy file is an explicit runtime input and may differ from the selected tree; the CLI does
not claim policy provenance. CI should check out the exact source commit, keep the workflow
unprivileged, and pass that same commit through a quoted environment variable:

```yaml
- name: Check committed-tree policy
  env:
    YAGA_TREE_REVISION: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}
  run: >-
    uv run yaga tree check
    --policy .yaga/tree-policy.toml
    --revision "$YAGA_TREE_REVISION"
    --format github
```

The Git boundary uses documented
[`rev-parse --verify --end-of-options`](https://git-scm.com/docs/git-rev-parse.html) type peeling
and recursive, full-tree, NUL-delimited
[`ls-tree`](https://git-scm.com/docs/git-ls-tree/2.42.0.html) enumeration. Git metadata and alternate
object-store resolution follows the documented
[`gitrepository-layout`](https://git-scm.com/docs/gitrepository-layout).

## Committed-path portability checks

`yaga path check` validates the leaf names in one exact committed tree against one explicit
portability policy. The frozen `windows-compatible-v1` profile catches names that ordinary Windows
tools cannot represent reliably, plus ASCII case aliases that can collapse on a case-insensitive
checkout:

```toml
# .yaga/path-policy.toml
path-policy-version = 1
profile = "windows-compatible-v1"
```

```bash
yaga path check \
  --policy .yaga/path-policy.toml \
  --revision HEAD
```

Schema version 1 requires exactly one `profile` or `rules` selection. The profile permanently
expands to all four version-1 rules. A narrower policy can select a nonempty unique subset; YAGA
normalizes selected rules to the documented fixed order:

```toml
path-policy-version = 1
rules = ["windows-characters", "windows-trailing", "windows-reserved"]
```

The rules have intentionally narrow, host-independent meanings:

- `windows-characters` rejects `<`, `>`, `:`, `"`, `\`, `|`, `?`, `*`, and control characters
  U+0001 through U+001F within a component.
- `windows-trailing` rejects a component ending in an ASCII space or period.
- `windows-reserved` rejects `CON`, `PRN`, `AUX`, `NUL`, `COM1` through `COM9`, `LPT1` through
  `LPT9`, and the documented superscript-1-through-3 variants, including those names followed by
  an extension.
- `ascii-case-collision` catches full leaf aliases and file-versus-directory prefix aliases using
  ASCII-only case normalization. It does not apply locale-sensitive Unicode case folding, and
  harmless directory-casing variants with distinct leaves are not findings.

These boundaries follow Microsoft's documented [Windows file and directory naming
rules](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file), but the profile does
not claim compatibility with every filesystem, Windows configuration, API namespace, checkout
root, or application. Version 1 deliberately omits ignore patterns, Unicode normalization,
macOS-specific analysis, host-dependent path-length guesses, 8.3 alias simulation, content and EOL
checks, and symlink-target inspection.

YAGA resolves exactly one commit and tree, then reads every recursive full-tree leaf name through
bounded NUL-delimited Git output. It preserves policy-relevant characters long enough to report
them safely, but rejects malformed UTF-8, absolute or structurally invalid paths, duplicates, and
inconsistent leaf topology as operational errors. Regular and executable files, symlinks, and
gitlinks are checked uniformly as names; gitlinks are never traversed. The selection is bounded to
50,000 paths, 4,096 UTF-8 bytes and 64 components per path, 64 MiB of Git output, and one shared
10,000,000-work-unit evaluation ceiling. Legacy graft overlays are rejected before resolution and
rechecked after enumeration. A shallow checkout is accepted when the selected objects exist.

Findings use `path.windows-character`, `path.windows-trailing`, `path.windows-reserved`, and
`path.ascii-case-collision`. Versioned JSON retains exact totals and per-code counts while storing
only the first 256 canonical diagnostics; text and escaped GitHub output show smaller bounded
previews. Exit `0` means the selected tree passes, exit `1` means policy findings, and exit `2`
means an input, policy, Git, malformed-tree, or resource-limit error. The policy is an explicit
runtime input and carries no provenance claim. This installed provider is outside both Action
import graphs and repository-plan v1.

CI should pass the exact checked-out commit through one quoted value:

```yaml
- name: Check committed path portability
  env:
    YAGA_PATH_REVISION: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}
  run: >-
    uv run yaga path check
    --policy .yaga/path-policy.toml
    --revision "$YAGA_PATH_REVISION"
    --format github
```

## Committed entry-mode checks

`yaga mode check` verifies the Git mode of every leaf in one exact committed tree. It catches
executable-bit drift that is easy to miss on a Windows checkout, and it can make symlink or
gitlink presence an explicit repository policy rather than an accidental platform-dependent
surprise:

```toml
# .yaga/mode-policy.toml
mode-policy-version = 1
default-allowed-modes = ["regular"]

[[path-overrides]]
pattern = "scripts/**"
allowed-modes = ["executable"]

[[path-overrides]]
pattern = "vendor/dependency"
allowed-modes = ["gitlink"]
```

```bash
yaga mode check \
  --policy .yaga/mode-policy.toml \
  --revision HEAD
```

Schema version 1 uses the closed canonical mode names `regular` (`100644`), `executable`
(`100755`), `symlink` (`120000`), and `gitlink` (`160000`). The default allowed-mode list is
required, nonempty, and unique. A policy may add up to 128 ordered overrides with unique
case-sensitive patterns and nonempty unique allowed-mode lists. Patterns are anchored, support
component-local `*` and whole-component `**`, and the first matching override replaces the
default. Evaluation shares one hard 10,000,000-work-unit ceiling.

YAGA resolves exactly one commit and tree, then strictly parses bounded, recursive,
NUL-delimited `ls-tree` mode, type, object-ID, and path records. It never fetches, reads tracked
content or symlink targets, follows a gitlink, or consults the worktree, index, or untracked files.
Structurally valid names remain checkable even when `path check` would report them as nonportable.
Malformed UTF-8, invalid topology, duplicate leaves, unsupported mode/type pairs, graft overlays,
and exhausted resource limits fail closed. The usual 4,096-byte, 64-component, 50,000-entry,
64 MiB, and 30-second committed-tree bounds apply. Shallow history is accepted when the selected
commit and tree objects exist.

A mismatch reports `mode.disallowed` in lexical path order. Versioned JSON retains exact entry,
per-mode, and finding counts while storing the first 256 canonical diagnostics. Text and escaped
GitHub reports use smaller bounded previews. Exit `0` means the tree passes, exit `1` means mode
findings, and exit `2` means an input, policy, Git, malformed-tree, or resource-limit error.

This policy observes only Git's committed entry kinds and executable bit, as documented by Git's
[data model](https://git-scm.com/docs/gitdatamodel.html). An allowed mode does not validate a
shebang, content, ACLs, ownership, symlink-target safety, `.gitmodules` consistency, submodule
provenance, or object availability. The explicit policy carries no provenance claim. This
installed provider remains outside both Action import graphs and repository-plan v1.

CI should pass the exact checked-out commit through one quoted value:

```yaml
- name: Check committed entry modes
  env:
    YAGA_MODE_REVISION: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}
  run: >-
    uv run yaga mode check
    --policy .yaga/mode-policy.toml
    --revision "$YAGA_MODE_REVISION"
    --format github
```

## Committed blob-size checks

`yaga size check` enforces per-file and aggregate byte budgets against one exact committed tree.
It complements `tree check`: tree policy controls which paths may exist, while size policy controls
the stored blob bytes behind those paths.

```toml
# .yaga/size-policy.toml
size-policy-version = 1
default-max-blob-bytes = 131072
max-total-blob-bytes = 8388608

[[path-limits]]
pattern = "uv.lock"
max-blob-bytes = 1048576
```

```bash
yaga size check \
  --policy .yaga/size-policy.toml \
  --revision HEAD
```

Schema version 1 requires the default blob limit, permits one optional total limit, and accepts up
to 128 ordered path overrides. Byte limits are integers from zero through 2^53 - 1. Patterns are
anchored and case-sensitive, with component-local `*` and whole-component `**`; the first matching
override wins. A total limit is independent of the selected per-path limit. Pattern evaluation has
one hard 10,000,000-work-unit budget. A logical aggregate above 2^53 - 1 bytes is an operational
error so every byte count remains exactly portable in JSON numbers.

YAGA resolves exactly one commit and reads bounded, NUL-delimited `ls-tree` metadata from its tree.
It does not fetch, read the worktree or index, inspect untracked files, or recurse into submodules.
Regular and executable files count their stored blob bytes; symlinks count the bytes in the stored
link target. Gitlinks are validated and reported as entries but excluded from the byte total. The
same blob referenced at two paths counts twice because the policy measures logical checkout size.
Git LFS pointer files count only their committed pointer bytes, not remote LFS object size. Legacy
graft overlays are rejected before revision resolution and rechecked after enumeration. A shallow
checkout is accepted when the selected commit, tree, and required blobs are already present.

Per-path violations report `size.blob` in lexical path order; one aggregate violation follows as
`size.total`. Text, versioned JSON, and escaped GitHub reports use exit `0` for a passing tree, exit
`1` for budget findings, and exit `2` for policy, revision, repository, Git, or resource-limit
errors. GitHub output attaches blob findings to files and emits the aggregate finding without a
file annotation.

The policy is an explicit runtime input and carries no provenance claim. This is an installed CLI
provider, not part of either Action runtime or repository-plan v1. CI should use the exact checked
out commit:

```yaml
- name: Check committed blob sizes
  env:
    YAGA_SIZE_REVISION: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}
  run: >-
    uv run yaga size check
    --policy .yaga/size-policy.toml
    --revision "$YAGA_SIZE_REVISION"
    --format github
```

## Pull-request checks in GitHub Actions

`yaga github pull-request check` applies the same commit policy to a GitHub event without calling
the GitHub API. It checks the pull-request title as a single Conventional Commit header, then checks
every commit in the exact event `base.sha..head.sha` range. Body and merge rules do not apply to the
title; all configured header, type, scope, and description rules do. Text, versioned JSON, and
escaped `--format github` annotations share exit codes `0`, `1`, and `2` with `commit check`.

When `dependabot-pull-requests = "skip"`, the adapter recognizes only a strictly parsed PR author
with login `dependabot[bot]` and account type `Bot`. Both the title and every selected commit are
reported as skipped with the reason `Dependabot pull request`, and the command exits `0`. A human
PR, a near-match account, malformed event data, a checkout mismatch, missing history, or an
over-limit range is never converted into a skip.

For local diagnosis, save a `pull_request` event and check out its exact head before running:

```bash
yaga github pull-request check --event-file event.json --repo .
```

The separate read-only Action wraps that command for CI:

```yaml
name: Commit Policy

on:
  pull_request:
    types: [opened, synchronize, reopened, edited, ready_for_review]

permissions:
  contents: read

concurrency:
  group: yaga-commit-policy-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  commit-policy:
    name: Commit Messages
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false
      - uses: dariuszpanas/yaga/actions/commit-check@cd02385e3216ac544e7783c7dd6929340e230e95
```

The head checkout and complete history are required: YAGA refuses a synthetic merge checkout,
shallow history, a missing object, an empty range, or a checkout that does not equal the event head.
The Action has no token input or write/API code path, and the YAGA runtime never fetches Git history
or installs YAGA dependencies; the reference workflow grants only `contents: read`. It strictly
binds the event name, repository ID/name, base/head refs, and open PR payload to the runner context,
strictly parses the PR author record used by the optional Dependabot policy, and caps workflow
annotations at 50. Its pinned `actions/setup-python` bootstrap receives an explicit empty token and
may obtain the declared Python 3.12 runtime before YAGA starts.

This remains an unprivileged PR check and therefore a quality signal, not a security authority. The
policy file comes from the PR-head worktree and can be changed by the PR; review policy changes like
any other code. Title validation is bound to the triggering event rather than the commit SHA. The
`edited` wake and per-PR cancellation reduce stale ordering, but do not turn title metadata into
immutable evidence. The copy-ready [commit-policy workflow](examples/commit-policy.yml) pins an
audited pre-release commit; review and deliberately replace that immutable SHA when adopting a
newer YAGA revision.

## GitHub workflow checks

`yaga workflow check` catches mutable third-party Action, reusable-workflow, job-container, and
service-container references before they become a supply-chain regression. With no paths it checks
direct `.yml` and `.yaml` children of `.github/workflows`; explicit file or directory selections
make examples and other workflow sets checkable with the same policy:

```bash
yaga workflow check
yaga workflow check .github/workflows examples --format github
```

External GitHub references must use a full lowercase 40-character commit SHA. Step-level local
Actions may use `./` or `$/` repository paths, while job-level local references must select one
direct `.github/workflows/*.yml` or `.yaml` reusable workflow. Docker Actions must use a lowercase
SHA-256 image digest. The scalar and mapping forms of `jobs.<job>.container` and every
`jobs.<job>.services.<service>.image` use that same literal digest policy. A quoted empty service
image is allowed because GitHub treats it as disabled; dynamic expressions remain unverifiable and
fail closed. Expressions, ambiguous paths, tags, branches, abbreviated or uppercase SHAs, and
references in the wrong step/job context fail policy. This is a lexical immutability check: it does
not verify registry availability, signatures, provenance, or vulnerability status. See GitHub's
[job and service container syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idcontainer)
and Docker's [digest-pinning behavior](https://docs.docker.com/reference/cli/docker/image/pull/#pull-an-image-by-digest-immutable-identifier).

The `$/` self-repository syntax requires github.com and Actions runner 2.336.0 or newer; use `./`
for GitHub Enterprise Server or older self-hosted runners. Prefer `$/` when it is available: `./`
executes from the checked-out workspace, so its integrity depends on an exact trusted checkout and
it must not consume untrusted pull-request code in a write-capable workflow.

The checker strictly bounds selected files, bytes, YAML nodes, depth, aliases, scalar sizes,
references, and diagnostics. Its reported reference count includes selected `uses` values and
container images. It composes a YAML node graph without constructing Python objects, so duplicate
keys and merge keys remain visible and fail closed. Text, versioned JSON, and escaped GitHub
annotations use exit `0` for success, `1` for policy findings, and `2` for discovery, input, YAML,
or resource-limit errors.

`yaga workflow security` applies a narrow, pure-Python trust policy to the same bounded workflow
selection. The default, frozen `recommended-v1` profile requires an explicit read-only top-level
`permissions` boundary, keeps write scopes at individual jobs, rejects `write-all`, requires named
secret handoff instead of `secrets: inherit`, and hardens `actions/checkout` under privileged
`pull_request_target` or `workflow_run`. In those workflows, `ref` and `repository` selections must
be literal rather than dynamic expressions, while `allow-unsafe-pr-checkout` must be absent or the
literal value `false`. These rules follow GitHub's
[secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use) and
[permission model](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions),
but intentionally remain a small policy checker rather than a replacement for CodeQL, Scorecard,
or a broader Actions security scanner.

The frozen `recommended-v1` profile exposes five independently selectable rule IDs:

- `permissions.explicit` requires a top-level permission boundary.
- `permissions.top_level_write` moves mapping write scopes from the workflow to individual jobs.
- `permissions.write_all` rejects scalar `write-all` at workflow or job scope.
- `secrets.inherit` requires reusable-workflow jobs to name each forwarded secret.
- `checkout.untrusted_ref` applies the privileged-checkout restrictions above.

The opt-in `recommended-v2` profile contains those same five rules and adds
`checkout.persist_credentials`. Every direct step that the runner resolves to the
`actions/checkout` repository must provide exactly one scalar `with.persist-credentials` value with
the literal spelling `false`; plain and quoted values are accepted. A missing input, another value,
an expression, a non-scalar value, or an ambiguous duplicate fails closed at the checkout step.
Input names must be scalar ASCII values so Unicode or environment-name collisions cannot shadow
the required setting. This rule covers direct `actions/checkout` calls only: a wrapper Action must
enforce and document its own credential handling.

The opt-in `recommended-v3` profile contains all six `recommended-v2` rules and adds
`permissions.pull_request_write`. Whenever a workflow has any `pull_request` trigger, this rule
rejects the exact `pull-requests: write` and `statuses: write` permissions at both workflow and job
scope. A mixed-event workflow remains pull-request-triggered, and a job-level `if` condition does
not prove that a writer is unreachable from untrusted pull-request code. Split such a writer into a
trusted publisher triggered by `pull_request_target` or `workflow_run`; those triggers are allowed
by this rule when the workflow has no `pull_request` trigger, while the profile's other security
rules still apply. This deliberately narrow rule does not cover other write scopes, alternate
tokens, reusable-workflow permission inheritance, or expressions.

```bash
yaga workflow security
yaga workflow security --profile recommended-v2
yaga workflow security --profile recommended-v3
yaga workflow security .github/workflows examples --format github
yaga workflow security \
  --rule permissions.explicit \
  --rule checkout.untrusted_ref
```

With no selection options, YAGA still selects the frozen `recommended-v1`; opting in to
`recommended-v2` or `recommended-v3` is an explicit policy migration. Adding another future
default requires another versioned profile instead of silently changing an existing gate. Repeat
`--rule` to replace the profile with one exact, unique custom rule set; do not combine custom rules
with `--profile`. Text, versioned JSON, and escaped GitHub output preserve the same `0`/`1`/`2`
success, finding, and operational-error contract as the immutable-reference checker.

`yaga workflow lint` complements that pure-Python reference policy with GitHub workflow syntax and
schema checks. It accepts the same paths as `workflow check`: no paths selects direct `.yml` and
`.yaml` children of `.github/workflows`, while explicit files or direct-child directories select
other workflow sets.

```bash
yaga workflow lint
yaga workflow lint .github/workflows examples --format github
```

Linting requires Docker and runs the exact
`rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667`
container. YAGA does not bind the checkout into that container. It builds a bounded synthetic
repository containing the selected workflows, their transitive local reusable workflows, the one
native actionlint configuration, and local Action metadata. For Action metadata, native precedence
is preserved (`action.yaml` before `action.yml`); declared runtime files are represented only by
zero-byte existence markers, so their source is neither copied nor parsed. At most one of
`.github/actionlint.yaml` and `.github/actionlint.yml` may exist, and YAGA passes that file to
actionlint explicitly. Ambiguous configurations fail instead of silently choosing one.

The deterministic snapshot is streamed as an archive into a private, labeled Docker volume through
a stopped staging container. Selected workflow content still reaches actionlint through standard
input, while the read-only snapshot lets native local-workflow, local-Action, and configuration
checks work without a host mount. Valid `$/` self-repository references are translated to `./` only
inside this private snapshot because pinned actionlint predates that GitHub syntax; source-marked
rewriting preserves locations and never changes the checked-out file or reported path. A scalar
spelling that cannot be translated exactly fails closed.

The lint container has networking disabled, a read-only root and snapshot, all capabilities
dropped, `no-new-privileges`, fixed CPU and memory ceilings, a PID limit, and no Docker log driver.
YAGA bounds discovery, support files, paths, YAML structure, archive entries and bytes, subprocess
trees, time, output, diagnostics, and decoded JSON. It force-removes and verifies every labeled
container and private volume, treating unconfirmed cleanup as an operational failure and naming the
generated resource that needs manual removal. The configured Docker daemon or context may be
remote and is therefore trusted with the bounded snapshot until cleanup is verified. An
uncatchable process termination can bypass cleanup; leftover resources can be found
with Docker's `label=io.yaga.actionlint.run` filter and should be inspected before removal. This is
a pinned adapter around native actionlint, not a pure-Python schema linter. Sanitized text,
versioned JSON, and escaped GitHub reports use exit `0` for success, `1` for lint findings, and `2`
for Docker, snapshot, cleanup, input, or malformed-tool-output failures.

All three workflow commands are installed-CLI providers, not composite Actions. Run them after
installing the locked YAGA environment in ordinary unprivileged CI. Keep immutable-reference,
security, and syntax diagnostics separate; none replaces another.

## Aggregate repository checks

`yaga repo check` runs a closed, explicitly selected set of the existing providers and keeps their
reports separate. Repeat `--check` with `commit`, `workflow`, `workflow-security`,
`workflow-lint`, `mode`, `path`, `size`, or `tree`; at least one is required. There is deliberately
no implicit `all` selection, so adding a future YAGA provider never changes an existing local
command or CI gate.

```bash
yaga repo check \
  --check commit \
  --check workflow \
  --check workflow-security \
  --check workflow-lint \
  --check mode \
  --check path \
  --check size \
  --check tree \
  --commit HEAD \
  --revision HEAD \
  --workflow-path .github/workflows \
  --workflow-path examples \
  --mode-policy .yaga/mode-policy.toml \
  --path-policy .yaga/path-policy.toml \
  --size-policy .yaga/size-policy.toml \
  --tree-policy .yaga/tree-policy.toml
```

For a stable local-and-CI selection, put only the repository policy in an explicit, versioned plan:

```toml
# .yaga/checks/ci.toml
plan-version = 2
checks = [
  "commit",
  "workflow",
  "workflow-security",
  "workflow-lint",
  "mode",
  "path",
  "size",
  "tree",
]
workflow-paths = [".github/workflows", "examples"]
workflow-security-profile = "recommended-v3"
mode-policy = ".yaga/mode-policy.toml"
path-policy = ".yaga/path-policy.toml"
size-policy = ".yaga/size-policy.toml"
tree-policy = ".yaga/tree-policy.toml"
```

Then keep invocation-specific state on the command line:

```bash
yaga repo check --plan .yaga/checks/ci.toml --commit HEAD --revision HEAD
```

YAGA never discovers a plan implicitly. `--plan` replaces the selection flags `--check`,
`--workflow-path`, `--workflow-security-profile`, `--workflow-security-rule`, and each committed-
tree `--*-policy`; combining them is an input error. `--repo`, `--config`, `--commit`, `--range`,
`--revision`, and `--format` remain runtime options. Plan version 1 remains frozen to the original
four providers and workflow keys. Plan version 2 adds the four committed-tree providers and the
exact `mode-policy`, `path-policy`, `size-policy`, and `tree-policy` keys. A policy key is required
exactly when its provider is selected. Files, arrays, strings, and portable forward-slash paths are
strictly bounded; unknown keys, unknown versions, absolute or parent-traversing paths, duplicate
selections, inconsistent policy/provider pairs, and profile/rule conflicts fail closed. Every plan
path resolves under `--repo`, never beside the plan. Plans have no includes, discovery, environment
interpolation, expressions, commands, or secret fields. Existing symlinks and junctions are
resolved before policy loading and cannot carry a plan policy path outside the runtime repository.

A plan changed by an untrusted pull request controls only that pull request's unprivileged quality
check. It is not a security authority and cannot replace trusted default-branch workflow,
permissions, ruleset, or merge-policy enforcement.

Providers execute once in canonical `commit`, `workflow`, `workflow-security`, `workflow-lint`,
`mode`, `path`, `size`, `tree` order regardless of option order. The commit provider accepts one
`--commit` or `--range` and defaults to `HEAD`; it never fetches or weakens history. Selecting any
committed-tree provider requires one exact `--revision`, while using `--revision` without one is an
input error. The aggregate resolves that commit and tree identity once, then lets each selected
provider perform its own strict enumeration and policy evaluation so provider-specific parsing and
resource contracts remain intact. It never infers `HEAD` for these providers or reuses `--commit`
as their revision. The pure workflow providers consume one bounded parse of the same workflow
bytes selected by repeatable `--workflow-path` options, while lint may add its bounded transitive
support snapshot. Omitting `workflow-lint` keeps the aggregate pure Python and does not require
Docker. Provider-specific arguments are rejected unless their provider is selected. Use
`--workflow-security-profile recommended-v1` for the frozen default,
`--workflow-security-profile recommended-v2` to require checkout credential persistence to be
disabled, `--workflow-security-profile recommended-v3` to additionally reject pull-request
workflows with status or pull-request write authority, or repeat `--workflow-security-rule` for an
exact custom selection in the aggregate command.

Text preserves one section per provider. Versioned JSON embeds each existing provider document
under a `repository_check` envelope. GitHub output uses balanced provider groups and one shared
50-annotation budget. YAGA continues independent providers after expected operational errors: exit
`0` means all passed, exit `1` means findings with no operational error, and exit `2` means at least
one operational error even if other providers found policy violations. Exit `0` and `1` reports go
to standard output; aggregate exit `2` reports go to standard error.

## Gate commands and the composite Action

The installed CLI exposes all retained operations:

```bash
yaga gate agent-review invalidate
yaga gate agent-review prepare
yaga gate agent-review authorize
yaga gate agent-review observe
yaga gate agent-review request
yaga gate agent-review finalize
```

These are not offline simulations: they require the same trusted GitHub environment, event payload,
permissions, operation-specific protected approval marker where applicable, and default-branch
provenance as the composite Action. The GitHub token remains environment-only and is never accepted
as a CLI argument.

The installed CLI uses Typer. The write-capable composite Action intentionally installs nothing and
runs with Python site packages disabled. A fixed standard-library bootstrap accepts the same
`gate agent-review <operation>` command path and calls the same dispatcher, keeping the trusted
Action import graph dependency-free and network-free.

## Agent review Action: why the workflow is split

Write-capable review automation must not execute pull-request-controlled code. YAGA therefore puts
two trusted, write-capable default-branch workflows around ordinary unprivileged PR CI:

1. [`examples/review-policy.yml`](examples/review-policy.yml) is a tiny `pull_request_target`
   invalidator. A real lifecycle boundary uses the native job/check name `Review Policy Boundary`;
   an ordinary title/body edit instead uses `Review Policy Metadata`, which is not a required
   context. YAGA verifies the live PR, unique head ownership, and commit-status capacity before
   writing `Agent Review=pending` and `CI Gate=pending`. It checks out no code, requests no review,
   and does not trigger on `closed`.
2. Normal unprivileged PR CI runs the repository's tests and ends at a check named
   `CI Prerequisites`.
3. [`examples/agent-review.yml`](examples/agent-review.yml) wakes on a completed `CI` or
   `YAGA Review Policy` run. It re-fetches and authenticates the wake, resolves the exact current CI
   and lifecycle pair, requires a unique ready PR, and never reads artifacts or cache. Failed CI
   publishes terminal errors without asking an agent. Successful CI routes one bounded request or
   observes only a review already started by the exact current-boundary YAGA request. If CI
   completes first, `prepare` waits for the lifecycle run for at most two minutes; a later lifecycle
   completion also provides a second reconcile wake.
   Before any status write, YAGA deterministically elects only the later completion wake (CI wins an
   exact timestamp tie), so either queue order makes progress without two processors churning the
   same boundary.
4. A final independent operation revalidates CI, the live lifecycle boundary, authorization, and
   exact-head agent evidence before publishing classic `CI Gate=success` last.

The configured display names `CI` and `YAGA Review Policy` are coupled to the publisher's
`workflows` trigger, while their authenticated file paths are coupled to `prerequisite-workflow` and
`lifecycle-workflow`. Keep the names and paths synchronized. The CI workflow must trigger on
`opened`, `synchronize`, `reopened`, and
`ready_for_review`, and use the exact human run-name
`YAGA CI <action> for #<pull-request> at base <full-base-SHA>`. YAGA authenticates those bounded
action, PR, and base fields before accepting a completed `workflow_run`; contributor text must not
appear in that title.

The CI result is deliberately a quota-saving prerequisite and heuristic under YAGA's beta trust
model, not a security authority: PR-controlled code can alter its own CI behavior. The protected
environment and owner identity protect YAGA's quota path; `Maintainer Approval`, required review,
and audits of write-capable workflows and integrations remain part of merge security.

All workflows have friendly human run titles. The names `Agent Review` and `CI Gate` are reserved
for classic commit statuses and must not be used as workflow, job, or check names.

## Quota firewall

Create the required repository Actions variable `YAGA_AGENT_OWNER_ID` with the immutable numeric
GitHub user ID allowed to request agent review directly. The generic template contains no
account-specific ID. Every other PR author is routed through a precreated `agent-review-approval`
environment.
Configure that environment with the quota owner as its sole required reviewer, enable prevent
self-review, disable administrator bypass, and store no secrets. Add the environment variable
`YAGA_AGENT_APPROVAL_MARKER=agent-review-approval:v1`. The template uses `deployment: false`, so the
protection applies without creating a deployment record.

GitHub required-reviewer environment protection is available for public repositories on supported
plans and for private or internal repositories on GitHub Enterprise, subject to GitHub's current
plan rules. Confirm the feature and the exact protection settings in a preflight, then prove the
wait and approval behavior on a canary before enabling YAGA for contributors.

The protected external flow is intentionally split. The per-PR `authorize-external` job cancels a
stale approval wait and, after environment approval, records only a non-triggering approval marker.
A separate per-PR worker never cancels an in-flight run; it re-reads that marker and posts a review
request only when one is still needed. Owner requests use the same non-cancelling worker directly.
Within that trusted mutex, YAGA posts at most one strictly marked quota-consuming request for a
lifecycle boundary. The marker binds the repository, PR, full head and base SHAs, lifecycle run,
and authorizing CI run/attempt. YAGA lists the complete bounded comment history before and after its
single POST and never blindly retries an ambiguous write. CI reruns reuse the existing boundary
request. Immediately before that POST, YAGA revalidates the candidate and approval and re-reads the
exact request plus admissible pending/outcome evidence. GitHub cannot make the last read and comment
POST atomic, so another actor can still start a review in that final interval; disabling automatic
reviews removes the normal provider-side source of that residual race.

Disable automatic agent reviews and drain every existing agent-review task before enabling YAGA's v2
publisher. YAGA is then the sole legitimate automatic requester and asks only after CI passes.
Canary both the owner request and protected-external approval plus request paths. Every accepted
eyes reaction or outcome, including on `opened`, must follow the exact current-boundary
Actions-owned YAGA request marker. Visible unsolicited connector activity fails closed without
posting a duplicate YAGA request. External approval never reuses unsolicited evidence: it
authorizes YAGA to create the exact request and nothing else.

The environment protects only YAGA's token. OpenAI's public GitHub documentation does not describe
a repository control that prevents a person or another app from directly posting an agent-review request.
Such a comment may still consume provider quota. If its activity is already visible, YAGA fails
closed rather than spending quota on a second request. A direct request can still race YAGA's final
read and comment POST, however, and provider outcomes do not identify the triggering comment. The
current request marker therefore supplies temporal correlation, not a native provider binding.
Only that protected route's exact YAGA marker authorizes an external-author request. Repository
permissions and provider-side access controls remain necessary.

## Action interface

| Input | Contract |
| --- | --- |
| `gate` | Closed selector; currently only `agent-review`. |
| `operation` | One of `invalidate`, `prepare`, `authorize`, `observe`, `request`, or `finalize`. |
| `github-token` | Token passed only through the environment. |
| `prerequisite-workflow` | Authenticated PR CI path; default `.github/workflows/ci.yml`. |
| `lifecycle-workflow` | Trusted invalidator path; default `.github/workflows/review-policy.yml`. |
| `owner-id` | Immutable direct-request author ID; other authors use environment approval. |
| `approval-marker` | Protected-environment capability used only by `authorize`; otherwise empty. |
| `request-timeout` | Bounded REST timeout; default `5` seconds. |
| `job-timeout-minutes` | Bounded action window; the templates use `15`. |

`prepare` exposes the closed `route` output (`skip`, `done`, `observe`, `owner`, `external`, or
`approved`) and an exact `pull_request_number` output used only for job concurrency. Candidate
identity is never passed through workflow outputs; every operation re-reads trusted API state after
any queue or environment wait.

## Evidence contract

YAGA accepts only exact configured agent identities and these bounded outcomes:

- a clean connector issue comment whose reviewed-commit marker resolves to the current head;
- a formal connector findings review whose native `commit_id` and reviewed-commit marker identify
  the current head; or
- the connector's `+1` reaction.

Every outcome must be strictly later than the exact current-boundary Actions-owned YAGA request
marker, including on the initial non-draft `opened` boundary. An eyes reaction is progress, not
success, and selects the non-commenting `observe` path only when it is likewise bound to that exact
request. Visible connector activity without the marker is an invariant violation: YAGA fails closed
without posting a duplicate request, and external approval never converts or reuses that unsolicited
evidence.

A PR-body reaction has no commit/base identifier of its own, and even a reviewed-commit marker does
not identify the triggering request. A delayed review of an older head can finish after a newer
boundary, so the YAGA marker establishes temporal correlation rather than a native provider
binding. Automatic reviews must be disabled and drained before activation, and no direct or other
integration-triggered Agent review can overlap YAGA. YAGA also requires trusted successful status
lineage for every older YAGA request before it can post or trust evidence for a newer boundary. If a
head changes while its request is unresolved or timed out, open a fresh PR. A deployment that cannot
prevent overlapping non-YAGA reviews must not enable this beta action.

A formal findings review completes YAGA's evidence check. Consumers must also enable GitHub required
conversation resolution so unresolved findings remain merge-blocking. Findings outside a resolvable
review thread require a later clean review if the repository wants them to block.

## Lifecycle and recovery

The invalidator handles `opened`, `synchronize`, `reopened`, `edited`, `ready_for_review`, and
`converted_to_draft` on the default branch. Ordinary title/body edits are local no-ops, receive the
native `Review Policy Metadata` name, and use a unique concurrency group so they cannot cancel or
replace the required `Review Policy Boundary` check. Base-changing edits revoke success; push a new
commit to obtain fresh CI. Draft transitions remain pending until ready.

The publisher runs only after a completed CI or lifecycle workflow. Its entry job accepts the exact
`.github/workflows/ci.yml` plus `pull_request` pair or the exact
`.github/workflows/review-policy.yml` plus `pull_request_target` pair. The lifecycle completion may
therefore run against the `main` base branch, while post-merge CI has event `push` and skips every
publisher job before YAGA runs. Fork head branches named `main` remain eligible. There are no
comment, review, schedule, merge-queue, or `closed` wakes.
A delayed invalidator re-reads the exact live PR before writing and therefore skips a PR that is
already closed. Closure cannot start a new publisher run, but it can race an already-running
worker's final live read and subsequent comment or status POST because GitHub REST provides no
transaction spanning those operations. A marked request or status can therefore land after close
and a request may consume quota; YAGA revalidates and compensates where possible, but does not
guarantee zero post-close writes.

Polling is bounded. A timeout publishes `Agent Review=error`; rerun CI after the agent or GitHub
recovers. There is no scheduled repair. Polling admits another evidence pass only while a fixed
terminal-error request/time tail remains. Commit-status writes are not transactional, so runner loss
can leave pending. A newer CI/lifecycle run supersedes older attempts, and every request or
terminal write revalidates the live open PR as closely as GitHub REST permits. YAGA also reserves
status slots before each lifecycle boundary. Comments, reviews, and reactions each use one complete
page; 100 or more records is treated as incomplete and fails closed. More than eight older YAGA
request boundaries also fail closed, so continue on a new PR. Commit-status reads likewise require
a complete page with fewer than 100 visible statuses across
all contexts, and terminal publication reserves the last two visible slots for its write and one
repair. Push a new commit before that page fills or before the longer per-context status ceiling is
approached; either condition otherwise leaves the gate pending and requires a new head SHA.

## Required repository policy

Require strict, up-to-date `Commit Messages`, `Maintainer Approval`, `Review Policy Boundary`,
`CI Prerequisites`, `Agent Review`, and `CI Gate` as applicable. The native lifecycle check makes a
runner/API failure block before a new boundary can inherit old classic green; the native CI check
also blocks failed or in-progress reruns. Keep required conversation resolution enabled. Only YAGA
publishes classic `CI Gate` after review succeeds.
Merge queues are unsupported because YAGA has no `merge_group` trigger or combined-head contract.

The beta writer is the shared GitHub Actions integration, not a dedicated YAGA GitHub App. Audit
every default-branch workflow and integration with `statuses: write` or `pull-requests: write`,
reserve every case-insensitive `Agent Review` alias and every `CI Gate` alias, and keep repository
Actions defaults read-only. The three comment-writing jobs use `pull-requests: write`; live GitHub
Actions evidence showed that `issues: write` alone received HTTP 403 for the PR conversation route.
A same-named Actions check cannot replace a classic status: when GitHub requires both, both must
pass. A collision can still create ambiguity or denial of service. A dedicated App selected as the
expected status source is future hardening.

This beta also assumes GitHub delivers every configured lifecycle event. If a reopen or other
same-head transition is not delivered, GitHub does not create the new native boundary check and an
older green result can remain visible. There is deliberately no scheduled repair wake; verify event
delivery in the canary and treat missing delivery as a deployment blocker.

Before rollout:

1. Remove every legacy observer, schedule, comment wake, and duplicate status writer.
2. Precreate and verify the protected environment and the two required variables described above.
3. Rename the ordinary CI terminal check to `CI Prerequisites`; add the exact prerequisite trigger
   set and `YAGA CI <action> for #<pull-request> at base <full-base-SHA>` run-name. Keep the source
   workflow display names `CI` and `YAGA Review Policy` synchronized with the publisher's
   `workflows` trigger, keep their file paths synchronized with `prerequisite-workflow` and
   `lifecycle-workflow`, and install both templates at their configured paths. For a default branch
   other than `main`, replace `branches: [main]` in the lifecycle workflow.
4. Freeze new PRs, reach zero open PRs, disable automatic reviews, and drain every existing agent-review
   task before enabling the publisher. Pin the audited YAGA SHA, then open fresh canaries for failed
   CI, the owner request, protected-external approval plus request, timeout/rerun, metadata and
   lifecycle transitions, close, and merge. Confirm the post-merge publisher run skips every job.
5. Require `Review Policy Boundary`, `CI Prerequisites`, `Agent Review`, and `CI Gate` only after the
   exact canary heads succeed and before normal contributor traffic.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development details and [SECURITY.md](SECURITY.md) for
private vulnerability reporting and the beta trust boundary. See [CHANGELOG.md](CHANGELOG.md) for
the user-facing release history.
