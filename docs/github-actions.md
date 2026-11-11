# GitHub Actions

YAGA publishes a single unified composite GitHub Action at the repository root (`action.yml`,
published on GitHub Marketplace as **YAGA Checks**), selected with
`uses: dariuszpanas/yaga@<audited-40-character-sha>` and controlled by `check` (defaulting to
`commit`). Each run prints a bounded `YAGA Checks` ASCII header (honoring `NO_COLOR`) and writes
an HTML-escaped report summary to `$GITHUB_STEP_SUMMARY`.

## Execution runtimes and trust split

To preserve YAGA's strict supply-chain and token boundaries while presenting one Marketplace-ready
interface, the root Action dispatches into three isolated runtime branches:

1. **`check: commit` (default, read-only tokenless fast path)**: Syncs YAGA's hash-locked
   `--no-dev` runtime into a temporary virtual environment outside the checkout with tokens unset
   and executes `YAGA_COMMIT_ACTION_RUNTIME=1` via `"$YAGA_RUNTIME_PYTHON" -I -m yaga` (isolated
   from `PYTHONPATH`, working-tree imports, Typer/CLI command modules, and `GITHUB_TOKEN`/`GH_TOKEN`).
2. **Installed CLI checks (`policy`, `pr-review`, `pr-triage`, `pr-description`, `repo`, `branch`, `change`,
   `workflow`, `workflow-security`, `workflow-lint`, `mode`, `path`, `size`, `tree`)**: Reuses the
   same hash-locked `--no-dev` virtual environment outside the checkout and runs
   `python -I -m yaga.cli_action`. Every mode rejects unused inputs and option-like values
   fail-closed with exit `2`. `github-token` is stripped before `uv sync` and passed as `GH_TOKEN`
   only when live `--pr` access is requested for `pr-review`, `pr-triage`, or `pr-description`; offline checks
   reject `github-token`.
3. **`check: gate` (write-capable stdlib fast path)**: Runs `YAGA_ACTION_RUNTIME=1` with
   `python -P -S -m yaga gate` (no site packages and no runtime dependency installation) for the
   six Agent review status-gate operations; see the [GitHub review adapter](github-review-adapter.md).

## Pull-request commit checks (`check: commit`)

`yaga github pull-request check` applies commit policy to a GitHub event without calling the GitHub
API. It checks the pull-request title as a single Conventional Commit header, then checks every
commit in the exact event `base.sha..head.sha` range. Body and merge rules do not apply to the
title; all configured header, type, scope, and description rules do. Text, versioned JSON, and
escaped `--format github` annotations share exit codes `0`, `1`, and `2` with `commit check`.

When `dependabot-pull-requests = "skip"`, the adapter recognizes only a strictly parsed PR author
with login `dependabot[bot]` and account type `Bot`. It then narrows the exemption per commit using
the [bounded metadata and signature checks](commit-policy.md): ordinary human additions and human
rebases are checked, while compatible commits with a verified pinned GitHub signature retain the
reason `Dependabot pull request`. Missing or failed verification runs normal checks. Mixed PRs
also check their title and optional proposed message. The command exits `1` if any checked result
fails. The strict boolean `dependabot-require-github-signature` defaults to `true`; setting it to
`false` keeps only the forgeable metadata filter. The GitHub signing key is shared, so its signature
alone is not exclusive proof of Dependabot authorship. Verification is offline, without a live
badge or revocation lookup. A human PR, a near-match account, malformed event data, a checkout
mismatch, missing history, or an over-limit range is never converted into a Dependabot skip.
The separate explicit `skip-pull-request-authors` exemption still skips whole PRs independently
of this option; it is not a per-commit signature policy for other bots.

For local diagnosis, save a `pull_request` event and check out its exact head before running:

```bash
yaga github pull-request check --event-file event.json --repo .
```

Because `check` defaults to `commit`, the root Action runs this read-only check with no extra
inputs:

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
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false
      - uses: dariuszpanas/yaga@0000000000000000000000000000000000000000 # replace with audited SHA
```

The head checkout and complete history are required: YAGA refuses a synthetic merge checkout,
shallow history, a missing object, an empty range, or a checkout that does not equal the event head.
The `commit` runtime accepts no token input and has no write/API code path, and YAGA never fetches
Git history or installs dependencies; the reference workflow grants only `contents: read`. It
strictly binds the event name, repository ID/name, base/head refs, and open PR payload to the
runner context, strictly parses the PR author record used by the optional Dependabot policy, and
caps workflow annotations at 50. Its pinned `uv` runtime bootstrap unsets `GITHUB_TOKEN` and
`GH_TOKEN` and syncs only the hash-locked `--no-dev` environment outside the checkout before YAGA starts.

This remains an unprivileged PR check and therefore a quality signal, not a security authority. The
policy file comes from the PR-head worktree and can be changed by the PR; review policy changes like
any other code. Title validation is bound to the triggering event rather than the commit SHA. The
`edited` wake and per-PR cancellation reduce stale ordering, but do not turn title metadata into
immutable evidence. The copy-ready [commit-policy workflow](https://github.com/dariuszpanas/yaga/blob/main/examples/commit-policy.yml) pins an
audited placeholder SHA; review and deliberately replace that immutable SHA when adopting YAGA.

## Opt-in trusted commit policy

The `commit` mode's optional `trusted-config` input selects one literal repository-relative
`.yaga.toml` or `pyproject.toml` path. This mode is for a read-only `pull_request_target` workflow
defined on the default branch. Keep `contents: read`, disable checkout credential persistence,
and execute only an audited pinned YAGA Action and trusted default-branch code. Never check out
or execute the PR tree in this workflow.

The workflow must check out the exact default-branch runner revision with complete history and fetch the PR head into
`refs/remotes/pull/NUMBER/head`. The Action requires checkout `HEAD` to equal runner `GITHUB_SHA`,
the runner ref to identify the event repository's default branch, and the fetched ref to match
the exact event head. It reads policy directly from a bounded regular Git blob at that trusted
revision, ignoring working-tree edits, PR policy, global configuration, and symlink targets.
Missing/shallow history, absent policy, stale heads, malformed events, or checkout mismatches fail
with exit 2. A moving main checkout that differs from the event's runner SHA also fails closed.
The Action never fetches automatically. Use the default checkout revision for a
`pull_request_target` run (omit checkout `ref`), rather than a moving literal `main` ref.

PRs targeting non-default branches are supported. On current GitHub.com,
[`pull_request_target` uses the default branch for runner SHA/ref regardless of the PR base](https://github.blog/changelog/2025-11-07-actions-pull_request_target-and-environment-branch-protections-changes/).
The adapter validates `GITHUB_BASE_REF` against the event's actual target branch and checks
exactly `event.base.sha..event.head.sha`. Those selection coordinates are independent of the
default-branch policy revision. A weaker policy on a release branch or the PR is never used.
If main advances after the event, the original runner SHA remains policy authority; a checkout
at the newer main SHA fails closed. This mode does not support older server behavior that runs
`pull_request_target` from a non-default target branch.

Retain a revision-aware concurrency group containing both PR number and head SHA, and subscribe
to `edited` as well as commit lifecycle events so title and body edits are checked. For django-ray,
retain the required job name `Commit Messages`. A successful message comparison alone does not
establish safe event/checkout integration.

In this trusted mode, `dependabot-pull-requests = "skip"` additionally requires the PR head
repository to equal the base repository and its branch to start with `dependabot/`. The exact
`dependabot[bot]` login, `Bot` type, bounded positive author ID, configuration, and complete range
validation remain mandatory. Forks, lookalikes, and other branches are checked normally.
The default `dependabot-require-github-signature = true` also requires offline verification of
each otherwise compatible commit against the pinned GitHub signing key. A trusted `gpgv`
executable must be available for an exemption; missing tooling or verification failure runs
normal checks. No token or API access is needed, and no keys are downloaded during the check.

Without `trusted-config`, the existing `pull_request`/PR-head policy mode is unchanged.
The `exit-code` output exposes 0 for success, 1 for policy findings, and 2 for invalid input.
The `commit` runtime stays isolated from CLI/Typer modules and has no token input, write permissions, API calls,
or review-publisher integration. Hosted synthetic positive/negative controls supplement the
consumer's real-event qualification; they do not authorize a production cutover.

## Pull-request review verdicts (`check: pr-review`)

Repositories using automated agent reviews (such as Codex, Copilot, or Claude) can evaluate
head-bound review state and optionally submit a commit-bound `APPROVED` or `CHANGES_REQUESTED`
verdict from a trusted default-branch `pull_request_target` workflow (see
[`examples/pr-review.yml`](https://github.com/dariuszpanas/yaga/blob/main/examples/pr-review.yml)):

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  with:
    persist-credentials: false
- name: Evaluate and submit pull-request review
  uses: dariuszpanas/yaga@0000000000000000000000000000000000000000 # replace with audited SHA
  with:
    check: pr-review
    policy: .yaga/agent-review-policy.toml
    triage-policy: .yaga/triage-policy.toml
    pr: ${{ github.event.pull_request.number }}
    repo: ${{ github.repository }}
    wait-seconds: 480
    submit-review: both
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

Precede this with `check: pr-triage`, `policy: .yaga/triage-policy.toml`, the same `pr`/`repo`,
and explicit `publish-comment: "true"` to post a bounded admission comment and stop on hold.
See the complete example and [triage guide](pull-request-triage.md) for `issues: write`, verified
publisher identity, trusted wake events, and the separate merge-enforcement boundary.

Enable *Allow GitHub Actions to create and approve pull requests* in repository Actions settings
when `submit-review` is `approve` or `both`. Draft pull requests skip review submission cleanly;
positive publication also requires the explicit trusted `triage-policy`. External PRs remain on
hold until a configured maintainer approves their current head and acknowledges the exact current
base and trusted policy. See
[Pull-request trust triage](pull-request-triage.md) before adopting the publisher;
see the [Agent review guide](agent-review-gate.md#pull-request-review-state) for policy options,
polling bounds, and asynchronous re-review wake patterns.

## Proposed merge messages

Repositories using the PR title and description as the default commit message can opt in:

```toml
[tool.yaga.commit]
pull-request-message = "title-and-body"
body-policy = "required"
body-min-words = 8
```

Use `[commit]` in `.yaga.toml`. The default `"title-only"` keeps existing behavior.
The additional check composes the literal title, a blank line, and the description, then applies
full commit policy: per-type body requirements, prose limits, breaking markers, footer rules,
and optional spelling. A missing or null description is empty. Markdown and HTML comments are
not stripped or interpreted; template text can satisfy structural limits without explaining a change.

GitHub supports title plus description for both
[squash commits](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/configuring-commit-squashing-for-pull-requests)
and [merge commits](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/configuring-commit-merging-for-pull-requests).
Choose this option to match your repository's intended message source. YAGA does not read or modify
GitHub merge settings. Rebase merges use existing commit messages; multiple enabled merge methods,
GitHub-generated text, and last-minute message edits can produce a different final commit.
This check validates the proposed event text, not the final merged object.

Subscribe to `edited` events as well as commit lifecycle events: changing a description does not
change the PR head SHA. Preserve the workflow's concurrency and trusted-policy protections.
The existing title and commit results remain separate. An enabled check adds `proposed_message`
inside the schema-v1 JSON `pull_request` object and includes it in result counts; the key is absent
in default mode. Text and GitHub summaries identify the additional proposed message explicitly.
Author exemptions apply to it only after normal event, checkout, policy, and range validation.
`merge-commits = "ignore"` does not skip it because no Git merge parents exist for proposed text.

## Action interface

Every input outside the selected `check` mode must remain empty; unused inputs fail closed with
exit `2`.

| Input | Contract |
| --- | --- |
| `check` | Mode selector; `commit` (default), `policy`, `pr-review`, `pr-triage`, `pr-description`, `repo`, `branch`, `change`, `workflow`, `workflow-security`, `workflow-lint`, `mode`, `path`, `size`, `tree`, or `gate`. |
| `trusted-config` | Optional literal repository-relative commit config path for `check=commit` on `pull_request_target`. |
| `policy` | Explicit versioned TOML policy path for `policy`, `pr-review`, `pr-triage`, `pr-description`, `branch`, `change`, `mode`, `path`, `size`, or `tree`. |
| `template` | Optional explicit Markdown heading template path for `check=pr-description`. |
| `file` | Offline snapshot JSON path for `pr-review`/`pr-triage` or Markdown body file for `pr-description`. |
| `pr` | Canonical HTTPS PR URL or positive PR number for live `pr-review`, `pr-triage`, or `pr-description` via `gh`. |
| `repo` | `[HOST/]OWNER/REPO` for numeric `pr` in `pr-review`/`pr-triage`/`pr-description`, or repository root directory for `policy`, `change`, `workflow*`, `mode`, `path`, `size`, `tree`, or `repo`. |
| `wait-seconds` | Bounded active-review poll budget (`0..600`) for live `check=pr-review`. |
| `submit-review` | Commit-bound PR review mode (`none`, `approve`, `request-changes`, or `both`) for live `check=pr-review`. |
| `triage-policy` | Explicit trusted maintainer admission policy for live `check=pr-review`; required before positive formal review publication. |
| `publish-comment` | Lowercase `true`/`false` for owned comment publication in live `check=pr-triage`; omitted means false. |
| `branch` | Exact short branch name for `check=branch`. |
| `range` | Exact `A..B` or `A...B` Git range for `check=change` or `check=repo`. |
| `revision` | Exact Git commit-ish for `mode`, `path`, `size`, `tree`, or `repo`. |
| `commit` | Commit-provider Git revision for `check=repo`. |
| `config` | Explicit `.yaga.toml` or `pyproject.toml` path for `check=repo`. |
| `plan` | Explicit versioned repository check plan TOML path for `check=repo`. |
| `checks` | Comma- or newline-separated provider list for `check=repo` when `plan` is omitted. |
| `workflow-paths` | Newline-separated workflow file or direct-child directory paths for `workflow*` or `repo`. |
| `workflow-security-profile` | Versioned profile (`recommended-v1`, `recommended-v2`, or `recommended-v3`) for `workflow-security` or `repo`. |
| `workflow-security-rules` | Comma- or newline-separated custom rules for `workflow-security` or `repo`. |
| `mode-policy` | Explicit mode policy TOML path for `check=repo` without `plan`. |
| `path-policy` | Explicit path policy TOML path for `check=repo` without `plan`. |
| `size-policy` | Explicit size policy TOML path for `check=repo` without `plan`. |
| `tree-policy` | Explicit tree policy TOML path for `check=repo` without `plan`. |
| `github-token` | Token passed via environment only for `check=gate` or live `pr` in `pr-review`/`pr-triage`/`pr-description`; rejected by offline checks. |
| `gate` | Closed gate selector for `check=gate`; defaults to `agent-review`. |
| `operation` | One of `invalidate`, `prepare`, `authorize`, `observe`, `request`, or `finalize` for `check=gate`. |
| `prerequisite-workflow` | Authenticated PR CI workflow path for `check=gate`; defaults to `.github/workflows/ci.yml`. |
| `lifecycle-workflow` | Trusted invalidator workflow path for `check=gate`; defaults to `.github/workflows/review-policy.yml`. |
| `owner-id` | Immutable GitHub user ID allowed to request directly in `check=gate`. |
| `approval-marker` | Protected-environment capability marker used by `authorize` in `check=gate`. |
| `request-timeout` | Bounded REST timeout (`1..30` seconds) for `check=gate`; defaults to `5`. |
| `job-timeout-minutes` | Bounded polling/finalization window (`15..360` minutes) required for `check=gate`. |
| `agent-review-policy-file` | Must remain empty in `check=gate`; nonempty values fail before API access. |

Outputs:

- `exit-code`: Emitted on every path (`0` pass, `1` policy findings or gate failure, `2` input or operational error).
- `route` and `pull_request_number`: Emitted only by `prepare` in `check=gate`; see the [GitHub review adapter](github-review-adapter.md).

## Other workflow checks and optional tools

See [workflow checks](workflow-checks.md) for immutable references, security profiles, and
Docker-backed syntax checks. Use [repository plans](repository-plans.md) to combine providers.

If your commit or description policy enables Typos, install the executable in the same job before
invoking YAGA:

```bash
cargo install typos-cli --version 1.49.0 --locked
```

YAGA checks both PR titles and full messages when `typos = "check"` and does not install optional
tools automatically. Trusted mode uses an isolated built-in dictionary and requires the executable
outside the checkout. See [commit policy](commit-policy.md#optional-typos-integration) for errors
and configuration.

For other dependency update bots or service accounts, configure an explicit
[`skip-pull-request-authors` list](configuration.md#explicit-pr-author-exemptions).
It exempts title and commit rules, including spelling, after normal event/head validation.

## Live dogfooding in this repository

Alongside the copy-ready templates in `examples/`, YAGA dogfoods `YAGA Checks` (`uses: ./`) across
its own pull-request workflows: `commit-policy.yml` (`check: commit` and `pr-description`), the
`YAGA Checks` job in `ci.yml` (`check: branch`, `change`, `policy`, and `repo` with `.yaga/checks/ci.toml`), and
`pr-review.yml` (`check: pr-triage` then `pr-review`). See [Contributing](contributing.md#pull-request-jobs-and-yaga-dogfooding)
for how those jobs separate Action dogfooding from internal Python build and fixture jobs.
