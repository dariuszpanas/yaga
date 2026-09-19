# Command index

Choose a command here, then read its linked reference for examples, policy schema, diagnostics,
and limits. Run `yaga <group> --help` for the installed version's exact options.

For installation maintenance, use [`yaga self update`](getting-started.md#update-or-uninstall)
or `yaga self update --dry-run` to verify a uv tool installation without updating it.

## Repository policy

| Command | Input | Reference |
| --- | --- | --- |
| `yaga commit check` | One message, file, stdin, commit, or range | [Commit policy](commit-policy.md) |
| `yaga config init` / `show` | Target repository or explicit configuration | [Configuration](configuration.md) |
| `yaga branch check` | Explicit short branch name and policy | [Branch policy](branch-policy.md) |
| `yaga change check` | Explicit Git range and policy | [Changed-path policy](change-policy.md) |
| `yaga tree check` | Explicit revision and policy | [Required and forbidden paths](tree-policy.md) |
| `yaga path check` | Explicit revision and policy | [Path portability](path-policy.md) |
| `yaga mode check` | Explicit revision and policy | [Entry modes](mode-policy.md) |
| `yaga size check` | Explicit revision and policy | [Blob sizes](size-policy.md) |
| `yaga repo check` | Explicit provider selection or plan, with runtime inputs | [Repository plans](repository-plans.md) |

## Workflows and CI

| Command | Purpose | Reference |
| --- | --- | --- |
| `yaga workflow check` | Immutable action and container references | [Workflow checks](workflow-checks.md) |
| `yaga workflow security` | Versioned permission and checkout policies | [Workflow checks](workflow-checks.md) |
| `yaga workflow lint` | Syntax checks through Docker actionlint | [Workflow checks](workflow-checks.md) |
| `yaga github pull-request check` | Exact event-bound title and commit validation | [GitHub Actions](github-actions.md) |

Evaluating YAGA for another repository? Start with [Assess and adopt in CI](ci-adoption.md).

## Optional review tools

| Command | Purpose | Reference |
| --- | --- | --- |
| `yaga commit quality` | Model-backed message advice | [Commit quality](commit-quality.md) |
| `yaga gate agent-review policy check` | Validate a named-lens policy | [Agent review plans](agent-review-gate.md) |
| `yaga gate agent-review policy plan` | Describe work for an external adapter | [Agent review plans](agent-review-gate.md) |
| `yaga gate agent-review policy template` | Create a pending receipt scaffold | [Agent review plans](agent-review-gate.md) |
| `yaga gate agent-review policy evaluate` | Validate and aggregate a receipt | [Agent review plans](agent-review-gate.md) |
| `yaga gate agent-review <operation>` | Coordinate a trusted GitHub review lifecycle | [GitHub review adapter](github-review-adapter.md) |

For common reporting and exit behavior, see [Usage modes](usage.md). For failures to run,
see [Troubleshooting](troubleshooting.md). Configuration commands and review operations have
their own documented output options; not every command supports every format.
