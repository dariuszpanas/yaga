# YAGA

YAGA is an extensible Python CLI for repository policy that runs the same checks locally and in CI.
Its first general-purpose feature is a configurable
[Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) checker. The original
Codex review gate remains available as an experimental GitHub Action and as a CLI command; it has
not been discarded or hidden behind copied scripts.

YAGA is pre-release. The CLI and configuration schema may still change. Action consumers must pin
the exact audited 40-character commit SHA they canaried rather than a branch or mutable tag.

## Install and explore

Python 3.12 or newer is required. From a checkout:

```bash
uv sync --group dev
uv run yaga --help
```

The distribution is named `yaga-cli`; the executable and Python package are both named `yaga`.
The `yaga` distribution name on PyPI belongs to an unrelated project.

The command tree starts with six deliberately separated groups:

```text
yaga commit check                  # validate one message, commit, or range
yaga config init                   # create a safe standalone starter policy
yaga config show                   # explain the effective policy and its source
yaga github pull-request check     # validate one exact GitHub PR event
yaga repo check --check commit     # aggregate an explicit provider set
yaga workflow check                # require immutable workflow references
yaga workflow security             # enforce a bounded Actions trust policy
yaga workflow lint                 # lint workflow syntax with pinned actionlint
yaga gate codex-review <operation> # run the retained review gate
```

## Conventional Commit checks

With no input option, YAGA checks `HEAD`. Exactly one explicit source may be selected:

```bash
yaga commit check --message "feat(cli): add configurable checks"
yaga commit check --file .git/COMMIT_EDITMSG
printf 'fix: preserve stdin\n' | yaga commit check --stdin
yaga commit check --commit HEAD~1
yaga commit check --range origin/main..HEAD
```

Range checks read complete commit messages from Git in oldest-first order. They do not fetch missing
history or invoke a shell, and they reject shallow repositories rather than silently weaken a
selection. An empty range, a missing ref, or a selection above the configured commit limit is an
operational error.

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
body-max-line-length = 100
merge-commits = "reject"         # ignore, check, or reject
ignored-headers = ['Revert "*"'] # bounded, case-sensitive glob patterns
max-commits = 64
```

A standalone `.yaga.toml` uses `config-version = 1` and `[commit]` instead of the two
`[tool.yaga...]` tables. Omit `allowed-types` or `allowed-scopes` to allow any value. An explicit
empty `allowed-scopes` list permits only unscoped messages; `allowed-types` must not be empty.
`yaga config show` prints every effective value and the source file, with `--format json` for tools.

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

For a new repository, YAGA can create a recommended standalone policy and immediately report its
effective values:

```bash
yaga config init --repo .
```

The starter permits the common Conventional Commit types, requires lowercase type and scope,
allows an optional project-defined scope, bounds headers and body lines, rejects merge commits, and
checks at most 64 commits per range. Initialization creates only `.yaga.toml`: it never edits
`pyproject.toml`, overwrites a path, or shadows a configuration already discovered for the target
directory. Use `--format json` when another tool needs the created path and effective policy.

Diagnostics have stable names such as `syntax.header`, `type.allowed`, `scope.required`,
`header.length`, `breaking.marker-pair`, `body.word-count`, `footer.required`, and
`footer.forbidden`. Text and versioned JSON reports use these exit codes:

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
    rev: e3870e19a9c2697d961309552b87e86c72f93c25
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

## Pull-request checks in GitHub Actions

`yaga github pull-request check` applies the same commit policy to a GitHub event without calling
the GitHub API. It checks the pull-request title as a single Conventional Commit header, then checks
every commit in the exact event `base.sha..head.sha` range. Body and merge rules do not apply to the
title; all configured header, type, scope, and description rules do. Text, versioned JSON, and
escaped `--format github` annotations share exit codes `0`, `1`, and `2` with `commit check`.

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
      - uses: dariuszpanas/yaga/actions/commit-check@e3870e19a9c2697d961309552b87e86c72f93c25
```

The head checkout and complete history are required: YAGA refuses a synthetic merge checkout,
shallow history, a missing object, an empty range, or a checkout that does not equal the event head.
The Action has no token input or write/API code path, and the YAGA runtime never fetches Git history
or installs YAGA dependencies; the reference workflow grants only `contents: read`. It strictly
binds the event name, repository ID/name, base/head refs, and open PR payload to the runner context,
and caps workflow annotations at 50. Its pinned `actions/setup-python` bootstrap receives an
explicit empty token and may obtain the declared Python 3.12 runtime before YAGA starts.

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
reports separate. Repeat `--check` with `commit`, `workflow`, `workflow-security`, or
`workflow-lint`; at least one is required. There is deliberately no implicit `all` selection, so
adding a future YAGA provider never changes an existing local command or CI gate.

```bash
yaga repo check \
  --check commit \
  --check workflow \
  --check workflow-security \
  --check workflow-lint \
  --commit HEAD \
  --workflow-path .github/workflows \
  --workflow-path examples
```

Providers execute once in canonical `commit`, `workflow`, `workflow-security`, `workflow-lint`
order regardless of option order. The commit provider accepts one `--commit` or `--range` and
defaults to `HEAD`; it never fetches or weakens history. The pure workflow providers consume one
bounded parse of the same workflow bytes selected by repeatable `--workflow-path` options, while
lint may add its bounded transitive support snapshot. Omitting `workflow-lint` keeps the aggregate
pure Python and does not require Docker. Commit source and configuration options are rejected
unless `commit` is selected, and workflow paths or security-selection options are rejected unless
their provider is selected. Use `--workflow-security-profile recommended-v1` for the frozen
default, `--workflow-security-profile recommended-v2` to require checkout credential persistence
to be disabled, `--workflow-security-profile recommended-v3` to additionally reject pull-request
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
yaga gate codex-review invalidate
yaga gate codex-review prepare
yaga gate codex-review authorize
yaga gate codex-review observe
yaga gate codex-review request
yaga gate codex-review finalize
```

These are not offline simulations: they require the same trusted GitHub environment, event payload,
permissions, operation-specific protected approval marker where applicable, and default-branch
provenance as the composite Action. The GitHub token remains environment-only and is never accepted
as a CLI argument.

The installed CLI uses Typer. The write-capable composite Action intentionally installs nothing and
runs with Python site packages disabled. A fixed standard-library bootstrap accepts the same
`gate codex-review <operation>` command path and calls the same dispatcher, keeping the trusted
Action import graph dependency-free and network-free.

## Codex review Action: why the workflow is split

Write-capable review automation must not execute pull-request-controlled code. YAGA therefore puts
two trusted, write-capable default-branch workflows around ordinary unprivileged PR CI:

1. [`examples/review-policy.yml`](examples/review-policy.yml) is a tiny `pull_request_target`
   invalidator. A real lifecycle boundary uses the native job/check name `Review Policy Boundary`;
   an ordinary title/body edit instead uses `Review Policy Metadata`, which is not a required
   context. YAGA verifies the live PR, unique head ownership, and commit-status capacity before
   writing `Codex Review=pending` and `CI Gate=pending`. It checks out no code, requests no review,
   and does not trigger on `closed`.
2. Normal unprivileged PR CI runs the repository's tests and ends at a check named
   `CI Prerequisites`.
3. [`examples/codex-review.yml`](examples/codex-review.yml) wakes on a completed `CI` or
   `YAGA Review Policy` run. It re-fetches and authenticates the wake, resolves the exact current CI
   and lifecycle pair, requires a unique ready PR, and never reads artifacts or cache. Failed CI
   publishes terminal errors without asking Codex. Successful CI routes one bounded request or
   observes only a review already started by the exact current-boundary YAGA request. If CI
   completes first, `prepare` waits for the lifecycle run for at most two minutes; a later lifecycle
   completion also provides a second reconcile wake.
   Before any status write, YAGA deterministically elects only the later completion wake (CI wins an
   exact timestamp tie), so either queue order makes progress without two processors churning the
   same boundary.
4. A final independent operation revalidates CI, the live lifecycle boundary, authorization, and
   exact-head Codex evidence before publishing classic `CI Gate=success` last.

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

All workflows have friendly human run titles. The names `Codex Review` and `CI Gate` are reserved
for classic commit statuses and must not be used as workflow, job, or check names.

## Quota firewall

Create the required repository Actions variable `YAGA_CODEX_OWNER_ID` with the immutable numeric
GitHub user ID allowed to request Codex directly. The generic template contains no account-specific
ID. Every other PR author is routed through a precreated `codex-review-approval` environment.
Configure that environment with the quota owner as its sole required reviewer, enable prevent
self-review, disable administrator bypass, and store no secrets. Add the environment variable
`YAGA_CODEX_APPROVAL_MARKER=codex-review-approval:v1`. The template uses `deployment: false`, so the
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

Disable Codex automatic reviews and drain every existing Codex task before enabling YAGA's v2
publisher. YAGA is then the sole legitimate automatic requester and asks only after CI passes.
Canary both the owner request and protected-external approval plus request paths. Every accepted
eyes reaction or outcome, including on `opened`, must follow the exact current-boundary
Actions-owned YAGA request marker. Visible unsolicited connector activity fails closed without
posting a duplicate YAGA request. External approval never reuses unsolicited evidence: it
authorizes YAGA to create the exact request and nothing else.

The environment protects only YAGA's token. OpenAI's public GitHub documentation does not describe
a repository control that prevents a person or another app from directly posting `@codex review`.
Such a comment may still consume provider quota. If its activity is already visible, YAGA fails
closed rather than spending quota on a second request. A direct request can still race YAGA's final
read and comment POST, however, and Codex outcomes do not identify the triggering comment. The
current request marker therefore supplies temporal correlation, not a native provider binding.
Only that protected route's exact YAGA marker authorizes an external-author request. Repository
permissions and provider-side access controls remain necessary.

## Action interface

| Input | Contract |
| --- | --- |
| `gate` | Closed selector; currently only `codex-review`. |
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

YAGA accepts only exact Codex connector identities and these bounded outcomes:

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
integration-triggered Codex review can overlap YAGA. YAGA also requires trusted successful status
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

Polling is bounded. A timeout publishes `Codex Review=error`; rerun CI after Codex or GitHub
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
`CI Prerequisites`, `Codex Review`, and `CI Gate` as applicable. The native lifecycle check makes a
runner/API failure block before a new boundary can inherit old classic green; the native CI check
also blocks failed or in-progress reruns. Keep required conversation resolution enabled. Only YAGA
publishes classic `CI Gate` after review succeeds.
Merge queues are unsupported because YAGA has no `merge_group` trigger or combined-head contract.

The beta writer is the shared GitHub Actions integration, not a dedicated YAGA GitHub App. Audit
every default-branch workflow and integration with `statuses: write` or `pull-requests: write`,
reserve every case-insensitive `Codex Review` alias and every `CI Gate` alias, and keep repository
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
4. Freeze new PRs, reach zero open PRs, disable automatic reviews, and drain every existing Codex
   task before enabling the publisher. Pin the audited YAGA SHA, then open fresh canaries for failed
   CI, the owner request, protected-external approval plus request, timeout/rerun, metadata and
   lifecycle transitions, close, and merge. Confirm the post-merge publisher run skips every job.
5. Require `Review Policy Boundary`, `CI Prerequisites`, `Codex Review`, and `CI Gate` only after the
   exact canary heads succeed and before normal contributor traffic.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development details and [SECURITY.md](SECURITY.md) for
private vulnerability reporting and the beta trust boundary.
