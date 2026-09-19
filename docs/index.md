---
hide:
  - toc
---

# YAGA documentation

![YAGA terminal Y logo](assets/branding/isometric_terminal_y_logo_transparent.png){ width="128" height="128" }

YAGA checks repository conventions locally and in CI: commit messages, branch names, changes,
committed files, and GitHub Actions workflows. You choose the checks and supply their inputs.

[Install and run your first check](getting-started.md){ .md-button .md-button--primary }
[Find a command](commands.md){ .md-button }

## New to YAGA?

Evaluating it for an existing repository? [Assess and adopt in CI](ci-adoption.md) maps problems
to capabilities, prerequisites, and integration steps.

1. **[Get started](getting-started.md):** install the CLI, inspect or create a policy, and check a commit.
2. **[Choose an adoption recipe](recipes.md):** add a local hook or an ordinary CI check.
3. **[Create a repository plan](repository-plans.md):** keep a repeatable set of checks once you need several.

## Find the reference you need

| Working on | Read |
| --- | --- |
| Message structure, bodies, footers, or spelling | [Commit policy](commit-policy.md) |
| Policy discovery and configuration keys | [Configuration](configuration.md) |
| Branch names, changes, or committed files | [Repository checks](repository-checks.md) |
| Workflow references, permissions, or syntax | [Workflow checks](workflow-checks.md) |
| Pull-request validation | [GitHub Actions](github-actions.md) |
| A failing command or missing prerequisite | [Troubleshooting](troubleshooting.md) |

[Usage modes](usage.md) explains where local commands, hooks, and CI fit together.
The [command index](commands.md) links each command to its detailed reference.

## Optional review tools

Deterministic checks work without a model or review service. If you need additional review,
see [commit quality advice](commit-quality.md) or [agent review plans and receipts](agent-review-gate.md).
The [GitHub review adapter](github-review-adapter.md) is experimental and requires a separate
trusted-workflow deployment.

## Project information

YAGA is beta. See the [changelog](changelog.md), [security model](security.md),
[contributor guide](contributing.md), and [release procedure](releasing.md).
