# YAGA

<img src="https://dariuszpanas.github.io/yaga/assets/branding/isometric_terminal_y_logo_transparent.png" alt="YAGA terminal Y logo" width="128" height="128">

**Repository conventions you can check locally and enforce in CI.**

YAGA helps teams turn recurring review feedback into repeatable, deterministic checks. Catch
inconsistent commit structure, unfinished pull-request descriptions, missing companion changes,
unwanted committed files, risky workflow settings, and stale agent reviews before they become
another review conversation. Start with one check and adopt more as your project needs them.

## What can YAGA help with?

| Problem | What YAGA checks |
| --- | --- |
| Commit messages and branch names follow inconsistent conventions | [Commit policy](https://dariuszpanas.github.io/yaga/commit-policy.html) and [branch naming](https://dariuszpanas.github.io/yaga/branch-policy.html) defined by your team |
| Pull requests omit useful context or leave template sections unfinished | [Description checks](https://dariuszpanas.github.io/yaga/pull-request-descriptions.html) for required content, headings, templates, and optional spelling |
| Agent reviews finish asynchronously or leave pull requests waiting for an approval verdict | [Pull-request review checks](https://dariuszpanas.github.io/yaga/agent-review-gate.html#pull-request-review-state) with `codex`, `copilot`, and `claude` presets, active-review polling, and opt-in `APPROVE` / `REQUEST_CHANGES` verdicts |
| A change lands without the related files your project expects | [Changed-path rules](https://dariuszpanas.github.io/yaga/change-policy.html), such as source changes accompanied by tests |
| Unwanted files, oversized blobs, or incompatible paths enter the repository | [Committed-file checks](https://dariuszpanas.github.io/yaga/repository-checks.html) for contents, sizes, names, and entry modes |
| GitHub Actions workflows drift from your security and maintenance rules | [Workflow checks](https://dariuszpanas.github.io/yaga/workflow-checks.html) for immutable references, permission and checkout rules, and syntax |
| Local checks and CI enforce different policies | An explicit [repository plan](https://dariuszpanas.github.io/yaga/repository-plans.html) used in both places |
| A small change needs a clear test and qualification plan | [Test planning](https://dariuszpanas.github.io/yaga/test-planning.html) selects suites and required variants from explicit changed paths |

Checks produce actionable text, versioned JSON, and escaped GitHub workflow annotations with stable
exit codes (`0` pass, `1` policy finding, `2` input or operational error). You choose the policies
and checks; YAGA does not require adopting the entire CLI at once.

## Quick start

### Local CLI (`yaga-cli`)

YAGA requires Python 3.12 or newer. Install the **`yaga-cli`** distribution from PyPI (its command
is **`yaga`**; the PyPI distribution named `yaga` is an unrelated project):

```bash
uv tool install --python 3.12 yaga-cli
yaga config init --repo .
yaga commit check
```

See [Get started](https://dariuszpanas.github.io/yaga/getting-started.html) for convenience
installers, `pipx`, virtual environments, and `pre-commit` setup. The core CLI is tested on
Python 3.12 through 3.15.

### GitHub Action (`YAGA Checks`)

Run YAGA in GitHub Actions with `uses: dariuszpanas/yaga@<commit-sha>` (published on GitHub
Marketplace as **YAGA Checks**). `check` defaults to `commit` and supports `pr-review`,
`pr-description`, `repo`, `branch`, `change`, `workflow`, `workflow-security`, `workflow-lint`,
`mode`, `path`, `size`, `tree`, and `gate`:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  with:
    ref: ${{ github.event.pull_request.head.sha }}
    fetch-depth: 0
    persist-credentials: false
- uses: dariuszpanas/yaga@<audited-40-character-sha>
```

See the [GitHub Actions guide](https://dariuszpanas.github.io/yaga/github-actions.html) and
copy-ready [workflow examples](https://github.com/dariuszpanas/yaga/tree/main/examples) for commit
policy, repository plans, and trusted `check: pr-review` verdict workflows.

## Documentation

- [Get started](https://dariuszpanas.github.io/yaga/getting-started.html) — install YAGA and run your first check.
- [Adoption recipes](https://dariuszpanas.github.io/yaga/recipes.html) — add local hooks, CI checks, and review gates to an existing repository.
- [Assess YAGA for CI](https://dariuszpanas.github.io/yaga/ci-adoption.html) — match your problem to a check and its prerequisites.
- [GitHub Actions](https://dariuszpanas.github.io/yaga/github-actions.html) — inputs, outputs, and trust boundaries for the unified `YAGA Checks` Action.
- [Command reference](https://dariuszpanas.github.io/yaga/commands.html) — complete CLI and report index.
- [Full documentation](https://dariuszpanas.github.io/yaga/) — policies, schemas, and operational guidance.

## Optional review tools

Beyond deterministic repository checks, YAGA offers:

- [Pull-request review checks, plans, and receipts](https://dariuszpanas.github.io/yaga/agent-review-gate.html) — evaluate head-bound Codex, Copilot, Claude, or custom bot reviews from snapshots or live `gh` state and optionally submit commit-bound pull-request reviews.
- [Model-backed commit advice](https://dariuszpanas.github.io/yaga/commit-quality.html) — optional Hugging Face or Amazon Bedrock prose feedback with separate model dependencies.
- [GitHub status-gate adapter](https://dariuszpanas.github.io/yaga/github-review-adapter.html) — experimental multi-stage Codex lifecycle publisher for trusted workflows.

## Project status

YAGA is beta. CLI and configuration contracts may change before `1.0.0`. Workflow
security checks cover specific documented rules, not a complete security audit; see the
[security model](https://dariuszpanas.github.io/yaga/security.html) for boundaries and reporting.

[Changelog](https://dariuszpanas.github.io/yaga/changelog.html) ·
[Contributing](https://dariuszpanas.github.io/yaga/contributing.html)

Licensed under BSD-3-Clause.
