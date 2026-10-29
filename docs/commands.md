# Command index

Start with the source you intend to check, then read the linked reference for its policy,
diagnostics, and limits. Run `yaga <group> --help` for the installed version's exact options.
Checks never imply an all-checks mode or automatically run another provider.

## Choose a source and prerequisites

"Offline" below describes checking after installation. Optional spelling requires an installed
Typos executable; Git-backed commands require local objects and never fetch. No command in the
first table needs GitHub credentials except an explicit description `--pr` fetch.

| Command and reference | Source and required selection | External tools and execution |
| --- | --- | --- |
| [`yaga commit check`](commit-policy.md) | One literal title/message, editor file, file, stdin, commit, or range; defaults to `HEAD`. One discovered or explicit commit config. | Offline. Git for history and `--edit`; Typos if spelling is enabled. |
| [`yaga branch check`](branch-policy.md) | Literal short name via `--name` and explicit `--policy`. | Offline; no Git. |
| [`yaga pull-request description check`](pull-request-descriptions.md) | File, stdin, or current PR; explicit `--policy`, optional `--template`. | File/stdin are offline. Only `--pr` uses authenticated `gh` and network to read current metadata; no checkout. Typos if enabled. |
| [`yaga change check`](change-policy.md) | Committed changed paths from explicit `--range A..B` or `A...B` and `--policy`. | Offline Git; complete history and a unique three-dot merge base. |
| [`yaga tree check`](tree-policy.md) | Committed path names; explicit `--revision` and `--policy`. | Offline Git; selected commit/tree must exist. |
| [`yaga path check`](path-policy.md) | Committed path portability; explicit `--revision` and `--policy`. | Offline Git; selected commit/tree must exist. |
| [`yaga mode check`](mode-policy.md) | Committed entry modes; explicit `--revision` and `--policy`. | Offline Git; selected commit/tree must exist. |
| [`yaga size check`](size-policy.md) | Committed blob sizes; explicit `--revision` and `--policy`. | Offline Git; selected commit/tree/blobs must exist. |
| [`yaga workflow check`](workflow-checks.md) | Current workflow files: explicit paths or documented `.github/workflows` defaults. | Offline; installed YAML parser, no external tool. |
| [`yaga workflow security`](workflow-checks.md) | Same current files; frozen `recommended-v1` default, optional explicit profile/rules. | Offline; installed YAML parser, no external tool. |
| [`yaga workflow lint`](workflow-checks.md) | Current workflows plus bounded transitive local workflow/Action support files. | Docker CLI and Linux daemon; may pull the pinned image. Container networking is disabled. The active context can be remote and receives the snapshot. |
| [`yaga github pull-request check`](github-actions.md) | Event snapshot, matching runner identity/checkout, complete commit history, and commit config. | Offline Git; no API/token. Typos if enabled; trusted `gpgv` for the opt-in signed Dependabot exemption, otherwise normal checks run. |
| [`yaga github pull-request description check`](pull-request-descriptions.md#github-actions) | Explicit `--event-file`, `--policy`, optional `--template`. | Offline; no Git/checkout. Typos if enabled. |
| [`yaga repo check`](repository-plans.md) | Explicit provider list or `--plan`; selected providers' runtime inputs. | Inherits each selected provider's tools and execution boundary. |

`tree`, `path`, `mode`, and `size` do not see staged, untracked, or working-file changes. A
`repo check --revision SHA` sets only their committed source; it does not make workflow checks
read that revision. Policies and workflow files remain current runtime inputs.

## Planning and optional review

| Command and reference | Source and required selection | External tools and execution |
| --- | --- | --- |
| [`yaga test policy check`](test-planning.md) | Explicit policy via `--file`. | Offline; no Git or test execution. |
| [`yaga test plan`](test-planning.md) | Explicit policy/profile and exactly one range or revision; revision requires a full profile. | Offline Git; no test execution. Range needs complete history; revision needs its commit/tree. |
| [`yaga test evaluate`](test-planning.md#evaluate-normalized-results) | Explicit plan/results and independent policy, binding, commit/tree, run, and attempt identities. | Offline; no Git, provider API, or test execution. |
| [`yaga commit quality`](commit-quality.md) | One message, file, stdin, commit, or range; default `HEAD`; quality config and optional provider overrides. | Optional provider extra. Hugging Face runs locally but may download models; cached `--offline` prevents downloads. Bedrock sends selected text remotely and needs AWS credentials/region/access. Git for history. |
| [`yaga gate agent-review policy check`](agent-review-gate.md) | Explicit named-lens policy via `--file`. | Offline; no agent execution or credentials. |
| [`yaga gate agent-review policy plan`](agent-review-gate.md) | Explicit named-lens policy via `--file`. | Offline; describes work for an external adapter, does not run it. |
| [`yaga gate agent-review policy template`](agent-review-gate.md) | Explicit named-lens policy via `--file`. | Offline; emits a pending receipt scaffold. |
| [`yaga gate agent-review policy evaluate`](agent-review-gate.md) | Explicit policy and adapter results via `--file` and `--results`. | Offline; validates receipts, does not authenticate external provenance. |
| [`yaga gate agent-review <operation>`](github-review-adapter.md) | Trusted GitHub environment/event, lifecycle evidence, permissions and applicable approval marker. | Network and environment-only GitHub token. May write comments/statuses; trusted default-branch workflows only. |

## Reports and execution surfaces

For ordinary checks, `0` means passed, `1` means findings, and `2` means the check could not
complete. Receipt evaluation uses `1` for failed or incomplete obligations. Reports use stdout;
structured operational errors use stderr. CLI usage errors may be plain text even with
`--format json`; see [exit-aware capture](troubleshooting.md#read-the-exit-code-first).

| Commands | Formats; exits | Repository plan | Bundled Action |
| --- | --- | --- | --- |
| `commit check`; `workflow check`, `security`, `lint`; `tree`, `path`, `mode`, `size` checks | Text, JSON, GitHub; `0/1/2` | Exactly these eight providers; tree/path/mode/size require plan v2. | None directly; commit policy is reused by the event-aware commit Action. |
| `repo check` | Text, JSON, GitHub; `0/1/2` with operational errors taking precedence | Runs a selected plan. | None. |
| `branch check`, `change check`, both description checks, `commit quality` | Text, JSON, GitHub; `0/1/2` | No. | None. |
| `github pull-request check` | Text, JSON, GitHub; `0/1/2` | No. | Read-only `actions/commit-check`. |
| `test policy check`, `test plan` | Text, JSON; `0/2` | No. | None. |
| `test evaluate` | Text, JSON; `0/1/2` | No. | None. |
| `gate agent-review policy check`, `plan` | Text, JSON; `0/2` | No. | None. |
| `gate agent-review policy template` | JSON scaffold; `0/2` | No. | None. |
| `gate agent-review policy evaluate` | Text, JSON, GitHub; `0/1/2` | No. | None. |
| `gate agent-review <operation>` | Lifecycle output; `GateError` returns `1`, not the policy-check error code | No. | Trusted write-capable root Action. Nonempty `agent-review-policy-file` is rejected. |

All installed commands can run in suitable ordinary CI with their stated prerequisites. The two
bundled Actions are narrow adapters, not wrappers for every checker. Quality advice remains
advisory; a plan is not evidence that tests or reviews ran. See [Usage modes](usage.md) and
[Assess and adopt in CI](ci-adoption.md) before choosing required gates.

## Copyable starting points

These Bash recipes assume the named policies/input files exist. Choose policies from the linked
references; YAGA does not discover standalone policies or fetch missing history.

### Before creating a commit

```bash
yaga commit check --file message.txt --config .yaga.toml
yaga branch check --policy .yaga/branch-policy.toml --name feat/parser
yaga workflow check .github/workflows
yaga workflow security .github/workflows --profile recommended-v3
yaga pull-request description check --policy .yaga/description-policy.toml --file pr-description.md
```

These select the prospective message, supplied branch name, current workflow files, and local
description. Committed-tree checks still need a commit containing the intended changes.

### A complete committed pull request

Set `YAGA_BASE_SHA` and `YAGA_HEAD_SHA` to the exact base/head commits already available locally.
Use two dots for branch-only commit messages and three dots for changed paths against the merge
base. This repository plan must select `commit` and at least one committed-tree provider:

```bash
: "${YAGA_BASE_SHA:?Set the exact PR base SHA}"
: "${YAGA_HEAD_SHA:?Set the exact PR head SHA}"
yaga repo check --plan .yaga/checks/ci.toml \
  --range "$YAGA_BASE_SHA..$YAGA_HEAD_SHA" --revision "$YAGA_HEAD_SHA"
yaga change check --policy .yaga/change-policy.toml \
  --range "$YAGA_BASE_SHA...$YAGA_HEAD_SHA"
```

Check out the intended head separately for working-file workflow checks. A last-commit-only range
can miss earlier source changes followed by a docs-only commit. YAGA contributors can use the
explicit local qualification override in [Contributing](contributing.md#development-loop).
For a plan without committed-tree providers, omit `--revision`.

### Descriptions when Actions are unavailable

The local command above works offline. To read a current published description, authenticate
`gh` for the intended host and supply the full canonical PR URL; no checkout is required:

```bash
: "${YAGA_PR_URL:?Set the canonical HTTPS pull-request URL}"
yaga pull-request description check --policy .yaga/description-policy.toml --pr "$YAGA_PR_URL"
```

Use `--pr 123 --repo OWNER/REPO` instead when choosing a numeric PR explicitly. This reads the
current body; it does not check event-bound commit history or edit the pull request.

## Authoring and installation helpers

[`yaga config show`](configuration.md) reads one discovered or explicit config;
[`yaga config init`](configuration.md) creates a new standalone config with no-overwrite semantics
and supports `--dry-run`. Both are offline, use text/JSON, and return `0/2`.
[`yaga commit template`](commit-policy.md#read-only-message-templates) prints an incomplete message
from explicit conventional components or plain policy; it is offline, text-only, and returns
`0/2`. None belongs to repository plans or the bundled Actions.

For installation maintenance, use [`yaga self update`](getting-started.md#update-or-uninstall)
or `yaga self update --dry-run` to verify a uv tool installation without updating it. Actual
updates invoke uv and may access the package index; this is maintenance, not a policy check.
