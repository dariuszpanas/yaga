# Committed blob-size checks

`yaga size check` enforces per-file and aggregate byte budgets against one exact committed tree.
It complements `tree check`: tree policy controls which paths may exist, while size policy controls
the stored blob bytes behind those paths.

```toml
# .yaga/size-policy.toml
size-policy-version = 1
default-max-blob-bytes = 131072
max-total-blob-bytes = 8388608

[[path-limits]]
pattern = "uv.lock"
max-blob-bytes = 1048576
```

```bash
yaga size check \
  --policy .yaga/size-policy.toml \
  --revision HEAD
```

Schema version 1 requires the default blob limit, permits one optional total limit, and accepts up
to 128 ordered path overrides. Byte limits are integers from zero through 2^53 - 1. Patterns are
anchored and case-sensitive, with component-local `*` and whole-component `**`; the first matching
override wins. A total limit is independent of the selected per-path limit. Pattern evaluation has
one hard 10,000,000-work-unit budget. A logical aggregate above 2^53 - 1 bytes is an operational
error so every byte count remains exactly portable in JSON numbers.

YAGA resolves exactly one commit and reads bounded, NUL-delimited `ls-tree` metadata from its tree.
It does not fetch, read the worktree or index, inspect untracked files, or recurse into submodules.
Regular and executable files count their stored blob bytes; symlinks count the bytes in the stored
link target. Gitlinks are validated and reported as entries but excluded from the byte total. The
same blob referenced at two paths counts twice because the policy measures logical checkout size.
Git LFS pointer files count only their committed pointer bytes, not remote LFS object size. Legacy
graft overlays are rejected before revision resolution and rechecked after enumeration. A shallow
checkout is accepted when the selected commit, tree, and required blobs are already present.

Per-path violations report `size.blob` in lexical path order; one aggregate violation follows as
`size.total`. Text, versioned JSON, and escaped GitHub reports use exit `0` for a passing tree, exit
`1` for budget findings, and exit `2` for policy, revision, repository, Git, or resource-limit
errors. GitHub output attaches blob findings to files and emits the aggregate finding without a
file annotation.

The policy is an explicit runtime input and carries no provenance claim. This is an installed CLI
provider, not part of either Action runtime or repository-plan v1. CI should use the exact checked
out commit:

```yaml
- name: Check committed blob sizes
  env:
    YAGA_SIZE_REVISION: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}
  run: >-
    uv run yaga size check
    --policy .yaga/size-policy.toml
    --revision "$YAGA_SIZE_REVISION"
    --format github
```

See [command index](commands.md) for other checks and [adoption recipes](recipes.md) for CI setup.
