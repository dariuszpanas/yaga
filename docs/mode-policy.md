# Committed entry-mode checks

`yaga mode check` verifies the Git mode of every leaf in one exact committed tree. It catches
executable-bit drift that is easy to miss on a Windows checkout, and it can make symlink or
gitlink presence an explicit repository policy rather than an accidental platform-dependent
surprise:

```toml
# .yaga/mode-policy.toml
mode-policy-version = 1
default-allowed-modes = ["regular"]

[[path-overrides]]
pattern = "scripts/**"
allowed-modes = ["executable"]

[[path-overrides]]
pattern = "vendor/dependency"
allowed-modes = ["gitlink"]
```

```bash
yaga mode check \
  --policy .yaga/mode-policy.toml \
  --revision HEAD
```

Schema version 1 uses the closed canonical mode names `regular` (`100644`), `executable`
(`100755`), `symlink` (`120000`), and `gitlink` (`160000`). The default allowed-mode list is
required, nonempty, and unique. A policy may add up to 128 ordered overrides with unique
case-sensitive patterns and nonempty unique allowed-mode lists. Patterns are anchored, support
component-local `*` and whole-component `**`, and the first matching override replaces the
default. Evaluation shares one hard 10,000,000-work-unit ceiling.

YAGA resolves exactly one commit and tree, then strictly parses bounded, recursive,
NUL-delimited `ls-tree` mode, type, object-ID, and path records. It never fetches, reads tracked
content or symlink targets, follows a gitlink, or consults the worktree, index, or untracked files.
Structurally valid names remain checkable even when `path check` would report them as nonportable.
Malformed UTF-8, invalid topology, duplicate leaves, unsupported mode/type pairs, graft overlays,
and exhausted resource limits fail closed. The usual 4,096-byte, 64-component, 50,000-entry,
64 MiB, and 30-second committed-tree bounds apply. Shallow history is accepted when the selected
commit and tree objects exist.

A mismatch reports `mode.disallowed` in lexical path order. Versioned JSON retains exact entry,
per-mode, and finding counts while storing the first 256 canonical diagnostics. Text and escaped
GitHub reports use smaller bounded previews. Exit `0` means the tree passes, exit `1` means mode
findings, and exit `2` means an input, policy, Git, malformed-tree, or resource-limit error.

This policy observes only Git's committed entry kinds and executable bit, as documented by Git's
[data model](https://git-scm.com/docs/gitdatamodel.html). An allowed mode does not validate a
shebang, content, ACLs, ownership, symlink-target safety, `.gitmodules` consistency, submodule
provenance, or object availability. The explicit policy carries no provenance claim. This
installed provider remains outside both Action import graphs and repository-plan v1.

CI should pass the exact checked-out commit through one quoted value:

```yaml
- name: Check committed entry modes
  env:
    YAGA_MODE_REVISION: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}
  run: >-
    uv run yaga mode check
    --policy .yaga/mode-policy.toml
    --revision "$YAGA_MODE_REVISION"
    --format github
```

See [command index](commands.md) for other checks and [adoption recipes](recipes.md) for CI setup.
