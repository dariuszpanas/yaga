# Contributing

Keep changes focused, test-backed, and explicit about the public command, configuration,
diagnostic, or Action boundary they affect.

## Development loop

```bash
uv sync --group dev
uv run make ci
```

The full gate covers formatting, lint, type checks, tests, repository policies, workflow linting,
documentation, and the package build. The documentation-only loop is:

```bash
make docs
make docs-serve
```

Zensical's strict clean build catches broken navigation and source links without relying on stale
build cache.

## Change and review rules

Use Conventional Commit subjects and preserve unrelated work in the checkout. Keep parser
structure separate from configurable policy, maintain stable diagnostic identifiers, and add
focused tests for every new contract. Read [CONTRIBUTING.md](https://github.com/dariuszpanas/yaga/blob/main/CONTRIBUTING.md)
before opening a pull request.

When changing the dependency set, update both `pyproject.toml` and `uv.lock`. Before pushing,
inspect the working and cached diffs and run the complete gate.

Record user-visible changes in [docs/changelog.md](changelog.md), the single release-history source
linked from the root `CHANGELOG.md`. Follow its Keep a Changelog convention. Do not turn it into a
raw commit log; summarize the outcome and call out migration or security consequences.
