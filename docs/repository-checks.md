# Repository checks

Choose a check by the input you want to validate. Each check runs independently; selecting
one does not silently enable the others.

| Input | Question | Reference |
| --- | --- | --- |
| One branch name | Does it follow permitted naming patterns? | [Branch policy](branch-policy.md) |
| A Git range | Do changes include required companion paths? | [Changed-path policy](change-policy.md) |
| One committed tree | Are required paths present and forbidden paths absent? | [Tree policy](tree-policy.md) |
| One committed tree | Are names compatible with the selected portability rules? | [Path policy](path-policy.md) |
| One committed tree | Are executable, regular, symlink, and gitlink modes permitted? | [Mode policy](mode-policy.md) |
| One committed tree | Do stored blobs fit the size limits? | [Size policy](size-policy.md) |
| Workflow YAML | Are references pinned, permissions acceptable, and syntax valid? | [Workflow checks](workflow-checks.md) |

## Pick the right snapshot

Branch checks take a name directly and do not inspect Git. Change checks compare an explicit
`A..B` or `A...B` range. Tree, path, mode, and size checks take an explicit `--revision` and
inspect committed objects, not staged or working-tree edits. Commit those edits before
expecting a snapshot check to see them. Git-backed checks never fetch missing history.

Each reference above includes a policy example, command, diagnostics, and limits. Policies are
explicit, versioned files; see [configuration](configuration.md#standalone-provider-policies)
for the configuration boundary.

## Combine checks when you need them

Use a [repository plan](repository-plans.md) to save a repeatable selection for local use and CI.
Branch and change checks run separately from that plan. For a worked progression from one
check to a repository gate, follow the [adoption recipes](recipes.md).
