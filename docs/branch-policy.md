# Branch-name checks

`yaga branch check` validates one explicit short branch name against one explicit versioned policy.
It does not inspect the current checkout, invoke Git, discover configuration, read an event payload,
or normalize remote and full-ref prefixes. That makes the same command deterministic in a local
hook, a CI workflow, or a built-wheel smoke test:

```toml
# .yaga/branch-policy.toml
branch-policy-version = 1
allowed-patterns = [
  "main",
  "feat/*",
  "fix/*",
  "dependabot/*/**",
]
```

```bash
yaga branch check \
  --policy .yaga/branch-policy.toml \
  --name feat/branch-policy
```

Schema version 1 accepts one through 64 unique, case-sensitive patterns. A component-local `*`
matches within one slash-delimited component, while a whole `**` component matches zero or more
components. Matching is anchored to the complete name. Patterns and names use a bounded portable
ASCII grammar that rejects whitespace, shell punctuation, unsafe component edges, `..`, and
`.lock` endings. Names additionally reject whole-name `HEAD`, case-insensitive `refs/` prefixes,
and exact 40- or 64-character hexadecimal lookalikes. The 244-byte name limit is YAGA's portable
policy boundary, not a claim about every Git host.

A syntactically invalid name reports `branch.syntax`; a valid name not admitted by the policy
reports `branch.allowed`. Text, versioned JSON, and escaped GitHub reports return exit `0` for a
match, exit `1` for either finding, and exit `2` for invocation, input, or policy errors. The JSON
report includes a bounded terminal-safe rendering of the selected name, the ordered allowed
patterns, and the first matching pattern.

For Actions, copy the untrusted context value into an environment variable and quote it as one
argument. Pull requests use `github.head_ref`; branch pushes use `github.ref_name`. Do not pass a
pull request's synthetic `<number>/merge` ref name or infer a branch from checkout state:

```yaml
- name: Check pull-request branch name
  if: github.event_name == 'pull_request'
  env:
    YAGA_BRANCH_NAME: ${{ github.head_ref }}
  run: >-
    uv run yaga branch check
    --policy .yaga/branch-policy.toml
    --name "$YAGA_BRANCH_NAME"
    --format github
```

See [command index](commands.md) for other checks and [adoption recipes](recipes.md) for CI setup.
