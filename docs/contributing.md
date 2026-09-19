# Contributing

The docs gate checks rendered local links and anchors, including README routes and the public
logo, against the built site. Run `uv run make docs` after changing documentation or navigation.

Keep changes focused, test-backed, and explicit about the public command, configuration,
diagnostic, or Action boundary they affect.

## Development loop

Use Python 3.12+ and uv 0.12.7+. The full gate also needs Git, GNU Make, Bash,
a running Docker engine with Linux containers, and Typos CLI 1.49.0. On Windows, put
Git for Windows Bash on PATH before the WSL launcher. `uv sync` installs Python dependencies only.


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

### Keep documentation responsibilities clear

The README explains the problems YAGA solves and routes readers into the documentation. Keep
command examples, policy schemas, diagnostics, limits, and deployment procedures in their
reference pages. Update that reference when behavior changes; add a README entry only when
there is a new user problem or capability worth discovering. The command index and CI-adoption
guide link to those references rather than repeating their contracts. Documentation contract
tests target the owning reference page, not the README. Recipes may show a focused example;
they should link to the reference for the complete rules.

Use Conventional Commit subjects and preserve unrelated work in the checkout. Keep parser
structure separate from configurable policy, maintain stable diagnostic identifiers, and add
focused tests for every new contract. Read [CONTRIBUTING.md](https://github.com/dariuszpanas/yaga/blob/main/CONTRIBUTING.md)
before opening a pull request.

When changing the dependency set, update both `pyproject.toml` and `uv.lock`. Before pushing,
inspect the working and cached diffs and run the complete gate.

Record user-visible changes in [docs/changelog.md](changelog.md), the single release-history source
linked from the root `CHANGELOG.md`. Follow its Keep a Changelog convention. Do not turn it into a
raw commit log; summarize the outcome and call out migration or security consequences.

## Build these docs locally

The documentation site is managed by [Zensical](https://zensical.org/docs/get-started/):

```bash
make docs
make docs-serve
```

`make docs` performs a clean strict build. `make docs-serve` starts the local preview server at
`http://localhost:9000` by default. Override the address when needed, for example
`make docs-serve DOCS_ADDR=127.0.0.1:8000`.

Without Make (including on Windows), run the same commands directly:

```bash
uv run zensical build --strict --clean
uv run zensical serve --dev-addr 127.0.0.1:9000
```

Open `http://127.0.0.1:9000` and leave the server running while editing: saved documentation
changes rebuild automatically. Press `Ctrl+C` in its terminal to stop the preview. The header's
theme control cycles between light, dark, and your system preference.

Every push to `main` publishes the clean build to [GitHub Pages](https://dariuszpanas.github.io/yaga/).
