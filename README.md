# YAGA

<img src="https://dariuszpanas.github.io/yaga/assets/branding/isometric_terminal_y_logo_transparent.png" alt="YAGA terminal Y logo" width="128" height="128">

**Repository conventions you can check locally and enforce in CI.**

YAGA helps teams turn recurring review feedback into repeatable checks. Catch inconsistent commit
structure, missing companion changes, unwanted files, and risky workflow settings before they
become another review conversation. Start with one check and adopt more as your project needs them.

## What can YAGA help with?

| Problem | What YAGA checks |
| --- | --- |
| Commit messages and branch names follow inconsistent conventions | [Commit policy](https://dariuszpanas.github.io/yaga/commit-policy.html) and [branch naming](https://dariuszpanas.github.io/yaga/branch-policy.html) defined by your team |
| A change lands without the related files your project expects | [Changed-path rules](https://dariuszpanas.github.io/yaga/change-policy.html), such as source changes accompanied by tests |
| Unwanted files, oversized blobs, or incompatible paths enter the repository | [Committed-file checks](https://dariuszpanas.github.io/yaga/repository-checks.html) for contents, sizes, names, and entry modes |
| GitHub Actions workflows drift from your security and maintenance rules | [Workflow checks](https://dariuszpanas.github.io/yaga/workflow-checks.html) for immutable references, permission and checkout rules, and syntax |
| Local checks and CI enforce different policies | An explicit [repository plan](https://dariuszpanas.github.io/yaga/repository-plans.html) used in both places |

Checks produce actionable findings and reports for automation. You choose the policies and checks;
YAGA does not require adopting the entire CLI at once.

## Start here

**[Install YAGA and run your first check](https://dariuszpanas.github.io/yaga/getting-started.html)**

YAGA requires Python 3.12 or newer. Its PyPI distribution is **`yaga-cli`** and its command is
**`yaga`**; the distribution named `yaga` is an unrelated project.

- [Adoption recipes](https://dariuszpanas.github.io/yaga/recipes.html) — add checks to an existing workflow.
- [Assess YAGA for CI](https://dariuszpanas.github.io/yaga/ci-adoption.html) — match your problem to a check and its prerequisites.
- [Documentation](https://dariuszpanas.github.io/yaga/) — policies, examples, and operational guidance.
- [Command reference](https://dariuszpanas.github.io/yaga/commands.html) — find the check for your task.

## Optional review tools

For feedback beyond deterministic rules, YAGA offers
[model-backed commit advice](https://dariuszpanas.github.io/yaga/commit-quality.html) and
[agent review plans and receipts](https://dariuszpanas.github.io/yaga/agent-review-gate.html).
These are optional. The experimental GitHub review adapter has separate deployment and security
requirements; it is not needed for ordinary local or CI checks.

## Project status

YAGA is beta. CLI and configuration contracts may change. Workflow security checks cover specific
rules, not a complete security audit; see the
[security model](https://dariuszpanas.github.io/yaga/security.html) for boundaries and reporting.

[Changelog](https://dariuszpanas.github.io/yaga/changelog.html) ·
[Contributing](https://dariuszpanas.github.io/yaga/contributing.html)

Licensed under BSD-3-Clause.
