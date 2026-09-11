# GitHub Actions

YAGA can run as an installed CLI or through its composite Actions. Keep those trust surfaces
separate.

## Pull-request checks

Pull-request CI is intentionally unprivileged. Check out the exact event head with complete
history, disable persisted credentials, and pass event values through quoted environment
variables. For example:

```yaml
- uses: actions/checkout@<audited-full-sha>
  with:
    ref: ${{ github.event.pull_request.head.sha }}
    fetch-depth: 0
    persist-credentials: false
- name: Check commits
  env:
    YAGA_RANGE: ${{ format('{0}...{1}', github.event.pull_request.base.sha, github.event.pull_request.head.sha) }}
  run: >-
    uv run yaga commit check --range "$YAGA_RANGE" --format github
```

If the repository policy sets `typos = "check"`, install the pinned Typos CLI before the check:

```yaml
- name: Install Typos CLI
  run: cargo install typos-cli --version 1.49.0 --locked
```

The adapter sends each complete selected commit message through Typos' stdin interface and uses
its JSON-lines output. Keep the version pinned and install it in the same job as YAGA; otherwise a
missing executable is an operational failure (exit `2`), not a skipped spelling check.

This workflow is a quota-saving quality heuristic, not a security authority: pull-request code
controls its own execution.

The repository's optional `commit-quality` workflow uses `--input-mode message` and
`--format github` for both its online cache-population run and its offline replay. This makes the
complete-message behavior explicit, including commit bodies. The workflow always runs the offline
replay even when the online advisory flags a message, then preserves the combined exit contract:
operational errors (`2`) take precedence over findings (`1`). Both bounded reports are copied into
`GITHUB_STEP_SUMMARY`; the online summary records whether the model cache was hit. The cache key
includes the runner OS, Python family, task, model identifier, and exact revision, and the workflow
passes those same variables to both inference commands so cache identity cannot drift from the selected
model. Do not cache executable files from an untrusted pull request. Keeping the input mode explicit
also prevents a future policy-default change from silently narrowing the workflow's coverage.

For an ordinary installed-CLI check, keep the source and checkout boundary explicit:

```yaml
- uses: actions/checkout@<audited-full-sha>
  with:
    fetch-depth: 0
    persist-credentials: false
- name: Check changed paths
  env:
    YAGA_CHANGE_RANGE: ${{ format('{0}...{1}', github.event.pull_request.base.sha, github.event.pull_request.head.sha) }}
  run: >-
    uv run yaga change check
    --policy .yaga/change-policy.toml
    --range "$YAGA_CHANGE_RANGE"
    --format github
```

The branch provider receives `github.head_ref` for pull requests and the branch-only
`github.ref_name` for pushes. Committed-tree providers receive the exact checked-out head through
`--revision`; they do not use the commit provider's `--commit` option or inspect staged and
untracked files. Keep these values in quoted environment variables rather than interpolating them
into shell source.

## Composite Action boundary

The root Action accepts only the closed inputs `gate`, `operation`, `github-token`,
`prerequisite-workflow`, `lifecycle-workflow`, `owner-id`, `approval-marker`, `request-timeout`,
`job-timeout-minutes`, and the optional `agent-review-policy-file`. The token is passed through the environment and never appears in
arguments, outputs, logs, or exceptions.
The root Agent-review step also captures its bounded stdout and stderr into an escaped
`GITHUB_STEP_SUMMARY` block while preserving the underlying operation exit code, so pending,
policy, and operational outcomes remain visible without opening raw logs.

For Agent review, `agent-review-policy-file` is resolved inside `GITHUB_WORKSPACE` and is
validated before any GitHub API request. Trusted publisher workflows should check out the
repository default branch with credentials disabled before passing a relative policy path; a
missing, malformed, or workspace-escaping policy fails closed. The policy controls review lenses
and aggregation only; it never supplies credentials or authorization.
The current GitHub adapter supports one `codex` lens with `review` outcome and `review` publication.
It rejects multi-lens, advisory, alternate-preset, or alternate-publication policies before the
first API request. Use `yaga gate agent-review policy plan --format json` and
`policy evaluate` with a separate trusted adapter when several agents or publication modes are
needed.

The write-capable root runtime uses `YAGA_ACTION_RUNTIME=1` and standard-library-only imports.
The separate read-only commit Action uses `YAGA_COMMIT_ACTION_RUNTIME=1`; it may use commit policy
modules but never Typer, the Agent review gate, REST transport, or site packages.

The Action preserves GitHub workflow annotations and writes the same bounded report, including
operational errors, to `GITHUB_STEP_SUMMARY` inside an escaped `<pre>` block. This makes a failed
commit-policy check readable from the run summary without changing its `0`/`1`/`2` exit contract.

## Workflow policy

Pin every third-party action to an audited full commit SHA. Keep permissions least-privilege and
audit every writer. `workflow security` intentionally does not emulate GitHub expressions; dynamic
privileged checkout inputs and ambiguous permissions fail closed.

For the retained write-capable review workflow, trusted default-branch `pull_request_target` or
`workflow_run` jobs are the only writers. They must not consume pull-request code, artifacts, or
cache.

### Choosing a workflow provider

Use the providers together when the repository needs all three guarantees:

| Provider | Answers | Needs Docker? |
| --- | --- | --- |
| `workflow check` | Are external actions and container images immutably referenced? | No |
| `workflow security` | Does the workflow satisfy the selected trust profile? | No |
| `workflow lint` | Does bounded actionlint parsing accept the workflow structure? | Yes |

The security profile is cumulative and opt-in by version: `recommended-v1` is the frozen default,
v2 adds literal `persist-credentials: false` on direct runner-resolved checkout steps, and v3 adds
the `pull_request` writer restriction for exact `pull-requests: write` and `statuses: write`
permissions. A custom `--rule` selection replaces the profile and cannot be combined with
`--profile`. A passing security check does not replace immutable-reference or syntax checks.

### Aggregate and plan modes

For a stable local-and-CI gate, select providers through a versioned plan:

```toml
plan-version = 2
checks = ["workflow", "workflow-security", "workflow-lint", "tree"]
workflow-paths = [".github/workflows", "examples"]
workflow-security-profile = "recommended-v3"
tree-policy = ".yaga/tree-policy.toml"
```

```bash
yaga repo check --plan .yaga/checks/ci.toml --commit HEAD --revision HEAD --format github
```

Plans never discover themselves, fetch, run commands, interpolate environment values, or become
security authority when changed by a pull request. `--plan` replaces provider-selection flags;
runtime repository, commit, revision, and output options remain on the command line. Selecting a
committed-tree provider requires one exact `--revision`, while `--commit` or `--range` belongs only
to the commit provider. Provider reports stay separate, and exit `2` wins over findings when any
selected provider has an operational error.
