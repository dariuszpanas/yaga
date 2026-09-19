# GitHub Actions

Use ordinary unprivileged CI for repository checks. The write-capable review adapter has a
separate [deployment guide](github-review-adapter.md); it is not required to use YAGA in CI.

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

See [commit policy](commit-policy.md#optional-typos-integration) for errors and configuration.
