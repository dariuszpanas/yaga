# Changed-path coupling checks

`yaga change check` enforces small repository relationships such as “Python source changes require
at least one test change.” Policy and Git range are both explicit; YAGA never guesses from the
working tree, discovers a policy, fetches history, or calls GitHub.

```toml
# .yaga/change-policy.toml
change-policy-version = 1

[[rules]]
name = "python-source-needs-tests"
when-any = ["src/**/*.py"]
require-any = ["tests/**/*.py"]
```

```bash
yaga change check \
  --policy .yaga/change-policy.toml \
  --range origin/main...HEAD
```

Version 1 supports only `when-any` plus `require-any`. A rule is skipped when no changed path
matches `when-any`, passes when triggered and at least one changed path matches `require-any`, and
otherwise reports `change.require_any`. Rule names are bounded lowercase slugs. Patterns are
case-sensitive, repository-relative POSIX paths with literal characters, component-local `*`, and
whole-component `**`; there are no exclusions, regexes, includes, expressions, environment
interpolation, or commands. Literal `$` and `%` stay literal.

Unique patterns are compiled and cached once per check. A shared hard budget of 10,000,000 matcher
work units bounds the combined pattern/path evaluation; exceeding it is an operational error with
exit `2`, so individually valid but excessively complex policies and diffs still fail closed.

YAGA accepts exactly one complete-history `A..B` or `A...B` range. Two dots compare the exact
endpoint trees. Three dots compare the unique merge base with the right endpoint, which matches the
usual pull-request view; ambiguous merge bases fail closed. Changed paths come from a bounded,
NUL-delimited, rename-disabled Git diff, so a rename is evaluated as a deletion plus an addition.
All change kinds count as matching paths in schema v1, including additions, modifications,
deletions, and gitlink updates; the policy deliberately does not infer file status or content.
Missing objects, shallow history, legacy graft overlays, invalid UTF-8 paths, unsafe paths, or an
over-budget diff are operational errors.

Text, versioned JSON, and escaped GitHub reports use exit `0` when every triggered rule passes,
exit `1` for coupling violations, and exit `2` for policy, range, repository, or Git failures. In an
ordinary unprivileged pull-request workflow, fetch complete history without persisting checkout
credentials and pass event SHAs through environment variables rather than interpolating them into
shell source:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  with:
    ref: ${{ github.event.pull_request.head.sha }}
    fetch-depth: 0
    persist-credentials: false
- name: Check changed-path coupling
  env:
    YAGA_CHANGE_RANGE: ${{ format('{0}...{1}', github.event.pull_request.base.sha, github.event.pull_request.head.sha) }}
  run: >-
    uv run yaga change check
    --policy .yaga/change-policy.toml
    --range "$YAGA_CHANGE_RANGE"
    --format github
```

This is an installed CLI check, not a privileged Action or merge-security authority. The caller is
responsible for installing its pinned YAGA environment and must not run pull-request-controlled
code from `pull_request_target` or `workflow_run` with trusted credentials.

See [command index](commands.md) for other checks and [adoption recipes](recipes.md) for CI setup.
