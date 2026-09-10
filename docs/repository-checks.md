# Repository checks

Repository checks describe either a change between two revisions or an invariant of one committed
tree. They never inspect untracked files or silently use the current working tree as policy input.

## Branch and change checks

`branch check` validates one explicit short branch name against one explicit schema-v1 policy:

```bash
yaga branch check --policy .yaga/branch-policy.toml --name feat/new-check
```

Patterns are ordered, case-sensitive, portable ASCII names. Component-local `*` and whole-
component `**` are supported. The first matching pattern is preserved in reports.

`change check` enforces relationships such as “Python source changes require a test change”:

```bash
yaga change check --policy .yaga/change-policy.toml --range origin/main...HEAD
```

Schema v1 has bounded `when-any` and `require-any` rules over repository-relative POSIX paths.
Two-dot ranges compare endpoint trees; three-dot ranges use one unambiguous merge base. Rename,
external diff, textconv, shallow-history, graft, unsafe-path, and matcher-work ambiguities fail
closed.

## Committed-tree checks

Each provider below requires a versioned policy and exact commit-ish:

| Provider | Enforces |
| --- | --- |
| `tree check` | Required paths and forbidden committed-tree patterns. |
| `path check` | Windows characters, trailing names, reserved names, and ASCII case collisions. |
| `mode check` | Allowed regular, executable, symlink, and gitlink entry modes. |
| `size check` | Per-blob and optional aggregate byte limits. |

Example:

```bash
yaga tree check --policy .yaga/tree-policy.toml --revision HEAD
yaga path check --policy .yaga/path-policy.toml --revision HEAD
yaga mode check --policy .yaga/mode-policy.toml --revision HEAD
yaga size check --policy .yaga/size-policy.toml --revision HEAD
```

These providers resolve one commit and tree through the shared bounded Git runtime, enumerate the
full committed tree, exclude gitlink traversal, and retain exact counts while bounding stored
diagnostics. Shallow repositories are allowed only when all selected objects exist.

## Workflow checks

`workflow check` enforces immutable action and container references. `workflow security` applies
the pure-Python trust policy; its default is the documented `recommended-v1`, while
`recommended-v2` adds literal `persist-credentials: false` for direct runner-resolved checkout
steps and `recommended-v3` additionally rejects exact `pull-requests: write` and `statuses: write`
under `pull_request` triggers.

`workflow lint` runs pinned actionlint in a hardened, network-disabled container. It stages only
selected workflows, transitively called local reusable workflows, one actionlint config, and the
zero-byte runtime presence entries needed by native Action metadata checks.

All workflow providers share one bounded workflow load and remain independent reports in `repo
check`. Local `$/` references are commit-bound; `./` references depend on the caller's trusted
checkout.
