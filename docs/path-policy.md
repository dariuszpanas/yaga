# Committed-path portability checks

`yaga path check` validates the leaf names in one exact committed tree against one explicit
portability policy. The frozen `windows-compatible-v1` profile catches names that ordinary Windows
tools cannot represent reliably, plus ASCII case aliases that can collapse on a case-insensitive
checkout:

```toml
# .yaga/path-policy.toml
path-policy-version = 1
profile = "windows-compatible-v1"
```

```bash
yaga path check \
  --policy .yaga/path-policy.toml \
  --revision HEAD
```

Schema version 1 requires exactly one `profile` or `rules` selection. The profile permanently
expands to all four version-1 rules. A narrower policy can select a nonempty unique subset; YAGA
normalizes selected rules to the documented fixed order:

```toml
path-policy-version = 1
rules = ["windows-characters", "windows-trailing", "windows-reserved"]
```

The rules have intentionally narrow, host-independent meanings:

- `windows-characters` rejects `<`, `>`, `:`, `"`, `\`, `|`, `?`, `*`, and control characters
  U+0001 through U+001F within a component.
- `windows-trailing` rejects a component ending in an ASCII space or period.
- `windows-reserved` rejects `CON`, `PRN`, `AUX`, `NUL`, `COM1` through `COM9`, `LPT1` through
  `LPT9`, and the documented superscript-1-through-3 variants, including those names followed by
  an extension.
- `ascii-case-collision` catches full leaf aliases and file-versus-directory prefix aliases using
  ASCII-only case normalization. It does not apply locale-sensitive Unicode case folding, and
  harmless directory-casing variants with distinct leaves are not findings.

These boundaries follow Microsoft's documented [Windows file and directory naming
rules](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file), but the profile does
not claim compatibility with every filesystem, Windows configuration, API namespace, checkout
root, or application. Version 1 deliberately omits ignore patterns, Unicode normalization,
macOS-specific analysis, host-dependent path-length guesses, 8.3 alias simulation, content and EOL
checks, and symlink-target inspection.

YAGA resolves exactly one commit and tree, then reads every recursive full-tree leaf name through
bounded NUL-delimited Git output. It preserves policy-relevant characters long enough to report
them safely, but rejects malformed UTF-8, absolute or structurally invalid paths, duplicates, and
inconsistent leaf topology as operational errors. Regular and executable files, symlinks, and
gitlinks are checked uniformly as names; gitlinks are never traversed. The selection is bounded to
50,000 paths, 4,096 UTF-8 bytes and 64 components per path, 64 MiB of Git output, and one shared
10,000,000-work-unit evaluation ceiling. Legacy graft overlays are rejected before resolution and
rechecked after enumeration. A shallow checkout is accepted when the selected objects exist.

Findings use `path.windows-character`, `path.windows-trailing`, `path.windows-reserved`, and
`path.ascii-case-collision`. Versioned JSON retains exact totals and per-code counts while storing
only the first 256 canonical diagnostics; text and escaped GitHub output show smaller bounded
previews. Exit `0` means the selected tree passes, exit `1` means policy findings, and exit `2`
means an input, policy, Git, malformed-tree, or resource-limit error. The policy is an explicit
runtime input and carries no provenance claim. This installed provider is outside both Action
import graphs and repository-plan v1.

CI should pass the exact checked-out commit through one quoted value:

```yaml
- name: Check committed path portability
  env:
    YAGA_PATH_REVISION: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}
  run: >-
    uv run yaga path check
    --policy .yaga/path-policy.toml
    --revision "$YAGA_PATH_REVISION"
    --format github
```

See [command index](commands.md) for other checks and [adoption recipes](recipes.md) for CI setup.
