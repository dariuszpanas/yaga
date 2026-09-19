# Committed-tree checks

Git execution uses the shared runtime with global `--no-lazy-fetch`; missing objects fail rather
than triggering a network fetch. See [usage modes](usage.md) for the common execution boundaries.

`yaga tree check` validates the tracked leaf paths in one explicit commit against one explicit
versioned policy. It is a snapshot invariant check: `change check` answers whether one delta has
the required companion changes, while `tree check` answers whether the resulting committed tree
contains required project files and excludes known local or generated artifacts.

```toml
# .yaga/tree-policy.toml
tree-policy-version = 1
required-paths = [
  ".yaga/tree-policy.toml",
  "LICENSE",
  "README.md",
  "SECURITY.md",
  "pyproject.toml",
  "uv.lock",
]
forbidden-patterns = [
  "**/.env",
  "**/__pycache__/**",
  "**/.venv/**",
  "**/*.pyc",
  "build/**",
  "dist/**",
]
```

```bash
yaga tree check \
  --policy .yaga/tree-policy.toml \
  --revision HEAD
```

Both arrays are required in schema version 1 and either may be empty, but the policy must contain
one through 128 entries in total. Required paths are exact. Forbidden patterns are anchored and
case-sensitive, with component-local `*` and whole-component `**`; a required path that is also
forbidden makes the policy invalid. Paths are canonical repository-relative UTF-8 POSIX names,
bounded to 4,096 bytes and 64 components. Selected trees are bounded to 50,000 leaf paths and
64 MiB of Git output. Pattern evaluation shares a hard 10,000,000-work-unit ceiling.

YAGA resolves the selected value as exactly one commit and enumerates its tree with bounded,
NUL-delimited Git output. It never fetches, reads tracked file contents, follows a submodule, or
consults the worktree, index, or untracked files. Regular and executable files, symlinks, and
gitlinks all count as leaf paths. Legacy graft overlays are rejected before revision resolution and
rechecked after enumeration. A shallow checkout is acceptable when the selected commit and tree
objects already exist. The runtime checks the outermost enclosing repository even when
`--repo` names a worktree subdirectory, plus bounded resolved `.git`, linked-worktree `commondir`,
object-directory, and local alternate-object-store metadata. Alternate chains are cycle-safe and
limited to 128 directories. Repository pointer and alternate metadata must be bounded regular files
that retain their identity while opened; special files fail closed before Git starts.
This is path policy, not secret-content detection.

Missing paths report `tree.required`; tracked paths matching the first configured forbidden
pattern report `tree.forbidden`. Text, versioned JSON, and escaped GitHub reports return exit `0`
for a clean tree, exit `1` for findings, and exit `2` for policy, revision, repository, Git, or
resource-limit errors. Reports bound displayed diagnostics while retaining exact checked-entry and
finding counts.

The policy file is an explicit runtime input and may differ from the selected tree; the CLI does
not claim policy provenance. CI should check out the exact source commit, keep the workflow
unprivileged, and pass that same commit through a quoted environment variable:

```yaml
- name: Check committed-tree policy
  env:
    YAGA_TREE_REVISION: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}
  run: >-
    uv run yaga tree check
    --policy .yaga/tree-policy.toml
    --revision "$YAGA_TREE_REVISION"
    --format github
```

The Git boundary uses documented
[`rev-parse --verify --end-of-options`](https://git-scm.com/docs/git-rev-parse.html) type peeling
and recursive, full-tree, NUL-delimited
[`ls-tree`](https://git-scm.com/docs/git-ls-tree/2.42.0.html) enumeration. Git metadata and alternate
object-store resolution follows the documented
[`gitrepository-layout`](https://git-scm.com/docs/gitrepository-layout).

See [command index](commands.md) for other checks and [adoption recipes](recipes.md) for CI setup.
