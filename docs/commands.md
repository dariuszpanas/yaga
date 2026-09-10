# Commands

The installed command groups are deliberately separated. Every command has bounded inputs,
stable text/JSON/GitHub reporting, and exit codes `0` for pass, `1` for policy findings, and `2`
for operational errors. Use `yaga <group> --help` for the exact current option surface.

| Command | Purpose | Typical source |
| --- | --- | --- |
| `yaga branch check` | Validate one short branch name. | `--policy ... --name ...` |
| `yaga change check` | Enforce changed-path coupling. | `--policy ... --range A...B` |
| `yaga commit check` | Check one message, commit, or range. | `--message ...` or `--range ...` |
| `yaga config init` | Create a starter commit policy. | `--repo .` |
| `yaga config show` | Show effective policy and source. | `--repo .` |
| `yaga github pull-request check` | Check one exact GitHub PR event. | event and checkout context |
| `yaga mode check` | Enforce committed entry modes. | `--policy ... --revision ...` |
| `yaga path check` | Check committed path portability. | `--policy ... --revision ...` |
| `yaga repo check` | Run an explicit provider plan. | `--plan ... --revision ...` |
| `yaga size check` | Enforce committed blob/total limits. | `--policy ... --revision ...` |
| `yaga tree check` | Enforce required/forbidden tree paths. | `--policy ... --revision ...` |
| `yaga workflow check` | Require immutable action references. | explicit workflow paths |
| `yaga workflow security` | Apply the versioned Actions trust policy. | explicit workflow paths |
| `yaga workflow lint` | Run pinned actionlint in a hardened container. | explicit workflow paths |
| `yaga gate codex-review <operation>` | Run the retained review-gate operation. | trusted Action context |

## Selection rules

`commit check` accepts exactly one explicit source—message, UTF-8 file, standard input, commit,
or range—and defaults to `HEAD` only when no source is supplied. A commit range is evaluated
oldest first. Git-backed commit checks require complete history and never fetch.

The branch provider takes one explicit short branch name and one explicit policy. The change
provider takes one exact `A..B` or `A...B` range and one explicit policy. The committed-tree
providers (`mode`, `path`, `size`, and `tree`) take one exact commit-ish and one explicit policy;
only `--repo` may default to the current directory. Workflow providers take explicit paths or
their documented default workflow selection.

## Aggregate checks

Repository checks do not imply an `all` provider set. Select providers explicitly, or use a
versioned plan checked into the repository:

```bash
uv run yaga repo check \
  --plan .yaga/checks/ci.toml \
  --commit HEAD \
  --revision HEAD
```

Committed-tree providers require an exact runtime `--revision`. The aggregate resolves that
identity once and keeps each provider's report separate.

Plans are opt-in and versioned. Plan v1 selects only the original commit, workflow,
workflow-security, and workflow-lint providers. Plan v2 can additionally select `mode`, `path`,
`size`, and `tree`, with one repository-relative policy path for each selected committed-tree
provider. There is no implicit `all` selection, plan discovery, includes, expressions, commands,
or environment interpolation.

## CI integration

For pull requests, check the exact event SHAs and use a complete checkout. Pass untrusted values
through quoted environment variables rather than interpolating them into shell source:

```yaml
- name: Check branch policy
  env:
    YAGA_BRANCH_NAME: ${{ github.head_ref }}
  run: >-
    uv run yaga branch check
    --policy .yaga/branch-policy.toml
    --name "$YAGA_BRANCH_NAME"
    --format github
```

The unprivileged pull-request workflow is a quality signal, not a security authority.
