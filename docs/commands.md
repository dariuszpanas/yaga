# Command reference

The installed command groups are deliberately separated. Each command below requires the
provider-specific policy and source options described in its `--help` output.

| Command | Purpose | Typical source |
| --- | --- | --- |
| `yaga branch check` | Validate one short branch name. | `--policy ... --name ...` |
| `yaga change check` | Enforce changed-path coupling. | `--policy ... --range A...B` |
| `yaga commit check` | Check one message, commit, or range. | `--message ...` or `--range ...` |
| `yaga config init` | Create a starter commit policy. | `--repo .` |
| `yaga config show` | Show effective policy and source. | `--repo .` |
| `yaga github pull-request check` | Check one exact GitHub PR event. | event and checkout context |
| `yaga mode check` | Enforce committed entry modes. | `--policy ... --commit ...` |
| `yaga path check` | Check committed path portability. | `--policy ... --commit ...` |
| `yaga repo check` | Run an explicit provider plan. | `--plan ... --revision ...` |
| `yaga size check` | Enforce committed blob/total limits. | `--policy ... --commit ...` |
| `yaga tree check` | Enforce required/forbidden tree paths. | `--policy ... --commit ...` |
| `yaga workflow check` | Require immutable action references. | explicit workflow paths |
| `yaga workflow security` | Apply the versioned Actions trust policy. | explicit workflow paths |
| `yaga workflow lint` | Run pinned actionlint in a hardened container. | explicit workflow paths |
| `yaga gate codex-review <operation>` | Run the retained review-gate operation. | trusted Action context |

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
