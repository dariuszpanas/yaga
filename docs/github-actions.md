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

This workflow is a quota-saving quality heuristic, not a security authority: pull-request code
controls its own execution.

## Composite Action boundary

The root Action accepts only the closed inputs `gate`, `operation`, `github-token`,
`prerequisite-workflow`, `lifecycle-workflow`, `owner-id`, `approval-marker`, `request-timeout`,
and `job-timeout-minutes`. The token is passed through the environment and never appears in
arguments, outputs, logs, or exceptions.

The write-capable root runtime uses `YAGA_ACTION_RUNTIME=1` and standard-library-only imports.
The separate read-only commit Action uses `YAGA_COMMIT_ACTION_RUNTIME=1`; it may use commit policy
modules but never Typer, the Codex gate, REST transport, or site packages.

## Workflow policy

Pin every third-party action to an audited full commit SHA. Keep permissions least-privilege and
audit every writer. `workflow security` intentionally does not emulate GitHub expressions; dynamic
privileged checkout inputs and ambiguous permissions fail closed.

For the retained write-capable review workflow, trusted default-branch `pull_request_target` or
`workflow_run` jobs are the only writers. They must not consume pull-request code, artifacts, or
cache.
