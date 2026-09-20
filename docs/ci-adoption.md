# Assess and adopt YAGA in CI

Use this guide when evaluating YAGA for an existing repository. Start with the problem you
want to catch, select the smallest useful check, and validate it locally before making it required.

## 1. Match your problem to a check

| Need | Choose | Required input | Additional prerequisites |
| --- | --- | --- | --- |
| Consistent commit messages | [Commit policy](commit-policy.md) | A message, commit, or range; one discovered or explicit policy | Git and complete history for Git sources; Typos only if enabled |
| Consistent branch names | [Branch policy](branch-policy.md) | A short name and policy file | None beyond the installed CLI |
| Companion files in a change | [Changed-path policy](change-policy.md) | An exact range and policy file | Git and complete history |
| Allowed contents, names, modes, or sizes | [Repository checks](repository-checks.md) | An exact revision and provider policy files | Git with the selected committed objects available |
| Pinned workflow references | [Workflow checks](workflow-checks.md) | Workflow paths or the documented default selection | None beyond the installed CLI |
| Permission and checkout rules | [Workflow security](workflow-checks.md) | Workflow paths and a selected security profile | None beyond the installed CLI |
| Workflow syntax | [Workflow lint](workflow-checks.md) | Workflow paths | Docker with Linux containers and access to the pinned image |
| One repeatable set of checks | [Repository plans](repository-plans.md) | A plan plus runtime commit/range/revision inputs | Requirements of every selected provider |

All of these are repository-quality checks. They do not request model reviews or need a GitHub
write token. Pin the released `yaga-cli` version in your CI installation so upgrades are deliberate.
See [installation](getting-started.md) for supported installation methods.

## 2. Confirm the boundary fits

- A path-coupling rule can require a changed test path; it cannot prove the tests cover a behavior.
- Snapshot checks inspect committed objects, not edits waiting in the index or working tree.
- Workflow security profiles enforce their documented rules, not every possible workflow risk.
- Local hooks are bypassable. PR-controlled CI is a quality signal, not independent security authority.

If you need behavior tests, secret scanning, or broader security analysis, retain those tools
alongside YAGA. Read only the chosen provider's reference for exact limits and exclusions.

## 3. Establish a local baseline

Inspect the target repository's existing policy before creating one. Run the selected check against
known good and known failing inputs. Review findings and agree on policy before requiring the job.
The [adoption recipes](recipes.md) provide concrete commands and policy examples.

For commit adoption, choose a [workflow starter](commit-policy.md#workflow-starters-and-type-explanations)
that matches how changes reach the main branch. Plain messages and Conventional Commits are explicit
choices. Selected policy findings can begin as [warnings](commit-policy.md#gradual-enforcement) while
input and operational failures remain blocking. If PR descriptions become merge messages, enable
[proposed-message checks](github-actions.md#proposed-merge-messages) as well.

Keep policy in the target repository. Reuse it in CI rather than duplicating policy arguments in
workflow files. A [repository plan](repository-plans.md) is useful once several checks need the
same selection; branch and change checks remain separate commands.

## 4. Wire an ordinary CI job

1. Install a pinned YAGA release and only the external tools your chosen checks need.
2. Check out the intended revision. For commit ranges and changed-path checks, provide complete
   history with the base and head available; YAGA will not fetch them.
3. Pass exact event values through quoted environment variables. Never interpolate untrusted
   branch names or revisions into shell source.
4. Run the same provider policies or plan that passed locally. Use `--format github` for GitHub
   annotations or `--format json` for other automation.
5. Preserve the exit status: `0` passes, `1` reports findings, and `2` means the check could not
   complete. Do not convert an operational failure into a pass.

For GitHub pull requests, choose between a range check and the
[event-aware commit Action](github-actions.md), which also validates PR titles and event context.
Use an unprivileged PR workflow with read-only permissions; no review publisher is needed.
After validation, configure your repository's branch protection to require the intended job.

## Optional capabilities are separate decisions

[Commit quality](commit-quality.md) adds model-backed advice and its own dependency/cache or
credential requirements. [Agent review plans](agent-review-gate.md) describe work for an external
adapter. Neither is needed to adopt the deterministic checks above.

The experimental [GitHub review adapter](github-review-adapter.md) writes comments and statuses
from trusted workflows and requires a separate security review and deployment canary. Do not add
it merely to obtain ordinary CI validation.
