# Repository checks

Repository checks describe either a change between two revisions or an invariant of one committed
tree. They never inspect untracked files or silently use the current working tree as policy input.

## Branch and change checks

`branch check` validates one explicit short branch name against one explicit schema-v1 policy:

```bash
yaga branch check --policy .yaga/branch-policy.toml --name feat/new-check
```

Patterns are ordered, case-sensitive, portable ASCII names. Component-local `*` and whole-

The name is validated independently of policy admission. Names use YAGA's bounded portable
grammar: unsafe whitespace and punctuation, `..`, invalid component edges, `refs/` prefixes,
whole-name `HEAD`, and hexadecimal object-ID lookalikes are rejected. A valid but unmatched name
reports `branch.allowed`; an invalid name reports `branch.syntax`. The provider does not inspect
the current checkout or infer a branch from Git, refs, events, or environment variables.

`change check` enforces relationships such as “Python source changes require a test change”:

```bash
yaga change check --policy .yaga/change-policy.toml --range origin/main...HEAD
```

Schema v1 has bounded `when-any` and `require-any` rules over repository-relative POSIX paths.
Two-dot ranges compare endpoint trees; three-dot ranges use one unambiguous merge base. Rename,
external diff, textconv, shallow-history, graft, unsafe-path, and matcher-work ambiguities fail
closed. A rule is skipped when no changed path matches `when-any`, passes when a required path
matches, and reports `change.require_any` when the relationship is missing. Patterns are literal,
case-sensitive paths with component-local `*` and whole-component `**`; there are no exclusions,
regular expressions, includes, commands, or environment interpolation. The shared matcher budget
is 10,000,000 work units.

Use `A..B` to compare the exact endpoint trees. Use `A...B` to compare the unique merge base to
the head, which is usually the useful pull-request view. The range must be explicit and the
checkout must contain complete, non-grafted history; YAGA never fetches. Changed gitlinks remain
visible and are not silently ignored.

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
diagnostics. `--revision` is the only snapshot selector; `--commit` belongs to the commit provider
and cannot be used here. The policy path is an explicit input and does not claim provenance from
the selected revision. Shallow repositories are allowed only when all selected objects exist.

### `tree check`: snapshot contents

The schema requires `required-paths` and `forbidden-patterns`. Required paths are exact; forbidden
patterns are anchored and case-sensitive. Both arrays may be empty, but their combined entry count
must be nonzero, and a required path may not be admitted by a forbidden pattern. The first matching
forbidden pattern is reported as `tree.forbidden`; missing exact paths report `tree.required`.

```toml
tree-policy-version = 1
required-paths = ["README.md", "SECURITY.md"]
forbidden-patterns = ["**/.env", "dist/**", "**/*.pyc"]
```

This is a path-and-presence check, not secret-content scanning. It reads Git tree metadata only;
it does not open tracked files, recurse into submodules, or inspect the index and worktree.

### `path check`: portability

Use the frozen `windows-compatible-v1` profile for all four rules, or select a nonempty unique
subset with `rules`: `windows-characters`, `windows-trailing`, `windows-reserved`, and
`ascii-case-collision`. The profile catches characters and reserved names Windows cannot represent
reliably, trailing spaces or periods, and ASCII case aliases that can collapse on a case-insensitive
checkout. It deliberately does not model Unicode normalization, locale-sensitive case folding,
8.3 aliases, host-dependent path lengths, file contents, or symlink targets.

Findings use stable codes `path.windows-character`, `path.windows-trailing`,
`path.windows-reserved`, and `path.ascii-case-collision`. Git-valid names are preserved long enough
to diagnose these policy findings; malformed UTF-8, unsafe topology, duplicates, and resource-limit
exhaustion are operational errors instead.

### `mode check`: entry kinds

The closed mode names map directly to Git modes: `regular` (`100644`), `executable` (`100755`),
`symlink` (`120000`), and `gitlink` (`160000`). `default-allowed-modes` is required and nonempty.
Ordered `path-overrides` replace the default for the first matching path, rather than adding to it:

```toml
mode-policy-version = 1
default-allowed-modes = ["regular"]

[[path-overrides]]
pattern = "scripts/**"
allowed-modes = ["executable"]
```

The provider checks Git's mode/type metadata only. An allowed executable mode does not validate a
shebang, permissions, ownership, ACLs, content, or provenance; a gitlink is reported as an entry
but never traversed.

### `size check`: stored bytes

The required `default-max-blob-bytes` applies to each regular, executable, or symlink blob. An
optional `max-total-blob-bytes` independently limits the logical aggregate, and ordered
`path-limits` provide first-match per-path exceptions:

```toml
size-policy-version = 1
default-max-blob-bytes = 131072
max-total-blob-bytes = 8388608

[[path-limits]]
pattern = "uv.lock"
max-blob-bytes = 1048576
```

Symlinks count their stored link-target bytes, duplicate blob IDs count once per path, Git LFS
pointer files count pointer bytes, and gitlinks are excluded from byte totals. Violations are
reported as `size.blob` followed by an aggregate `size.total` finding. Limits and JSON numbers are
bounded to the portable integer range through `2^53 - 1`.

All committed-tree providers enumerate at most 50,000 entries from bounded NUL-delimited Git
output, preserve exact totals while limiting stored diagnostics, and share a 10,000,000-unit
pattern-work ceiling. They reject legacy graft overlays and disable lazy fetching.

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
