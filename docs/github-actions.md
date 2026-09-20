# GitHub Actions

## Opt-in trusted commit policy

The commit-check Action's optional `trusted-config` input selects one literal repository-relative
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

Without `trusted-config`, the existing `pull_request`/PR-head policy mode is unchanged.
The `exit-code` output exposes 0 for success, 1 for policy findings, and 2 for invalid input.
The runtime remains dependency-free and has no token input, write permissions, API calls, or
review-publisher integration. Hosted synthetic positive/negative controls supplement the
consumer's real-event qualification; they do not authorize a production cutover.

Use ordinary unprivileged CI for repository checks. The write-capable review adapter has a
separate [deployment guide](github-review-adapter.md); it is not required to use YAGA in CI.

## Proposed merge messages (development toward 0.2.0)

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
immutable evidence. The copy-ready [commit-policy workflow](https://github.com/dariuszpanas/yaga/blob/main/examples/commit-policy.yml) pins an
audited pre-release commit; review and deliberately replace that immutable SHA when adopting a
newer YAGA revision.

## Other workflow checks

See [workflow checks](workflow-checks.md) for immutable references, security profiles, and
Docker-backed syntax checks. Use [repository plans](repository-plans.md) to combine providers.

If your commit policy enables Typos, install the executable in the same job before invoking YAGA:

```bash
cargo install typos-cli --version 1.49.0 --locked
```

The commit Action checks both PR titles and full messages when `typos = "check"`.
Install Typos before the Action; it does not install optional tools automatically.
Trusted mode uses an isolated built-in dictionary and requires the executable outside the checkout.
See [commit policy](commit-policy.md#optional-typos-integration) for errors and configuration.

For other dependency update bots or service accounts, configure an explicit
[`skip-pull-request-authors` list](configuration.md#explicit-pr-author-exemptions).
It exempts title and commit rules, including spelling, after normal event/head validation.
