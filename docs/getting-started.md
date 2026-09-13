# Get started

This guide covers **YAGA 0.1.0 (Beta)**. YAGA requires Python 3.12 or newer. Install the **`yaga-cli`** distribution from PyPI;
the executable and import package are **`yaga`**. The `yaga` distribution is an unrelated project.

## Install as a global tool

A tool installer makes `yaga` available from any repository without adding it to each project's
dependencies. Choose one installation method below.

### uv (recommended)

[Install uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
uv tool install --python 3.12 yaga-cli
uv tool update-shell
```

uv installs YAGA in an isolated environment for your user account and can download Python 3.12
if needed. Open a new terminal after updating `PATH`, then run `yaga --help`.

If the command is not found, `uv tool dir --bin` shows the directory to put on your `PATH`.
Use `Get-Command yaga` in PowerShell or `command -v yaga` on macOS/Linux to find which executable
is selected. `uv tool list` lists installed tools.

### pipx

With [pipx](https://pipx.pypa.io/stable/installation/) and Python 3.12 or newer installed:

```bash
pipx install yaga-cli
pipx ensurepath
```

Open a new terminal and run `yaga --help`. pipx also uses an isolated environment and exposes
the command on your user account's `PATH`. If pipx selects an older Python, pass
`--python /path/to/python3.12` with the actual interpreter path.

### pip in a virtual environment

Use pip when you prefer to manage the Python environment yourself. With Python 3.12 or newer:

```bash
python -m venv .venv
```

Activate it with `source .venv/bin/activate` on macOS/Linux or `.venv\Scripts\Activate.ps1`
in PowerShell, then install:

```bash
python -m pip install yaga-cli
yaga --help
```

This command is available while that environment is active. For a command available across
terminals without activation, use uv or pipx above.

### Use it in your project

Change into the repository you want to check, replacing the example path below:

```bash
cd /path/to/your-project
yaga config init --repo .
yaga config show --repo .
yaga commit check --message "feat: add a useful feature"
yaga commit check --commit HEAD
```

`config init` is only needed if the target repository has no discovered YAGA policy. Existing
policies should be inspected with `config show`. The message example passes the starter policy;
an existing stricter policy can require additional content. Git-backed checks require Git on
`PATH` and the selected history locally. `workflow lint` additionally requires Docker.

Run **`yaga` directly** with this installation. `uv run yaga` is the project-environment form
used when developing YAGA or when a target project explicitly depends on it. The global tool
still discovers policy from the repository being checked, not from its installation directory.

### Update or uninstall

Use the same installer you chose originally:

| Installer | Update | Uninstall |
| --- | --- | --- |
| uv | `uv tool upgrade yaga-cli` | `uv tool uninstall yaga-cli` |
| pipx | `pipx upgrade yaga-cli` | `pipx uninstall yaga-cli` |
| pip (active environment) | `python -m pip install --upgrade yaga-cli` | `python -m pip uninstall yaga-cli` |

Uninstalling leaves your repositories and their policy files in place. For repeatable installs,
use `"yaga-cli==X.Y.Z"` with a published version in place of `yaga-cli`. uv upgrades respect the
original version constraint; install again with a new constraint when changing a pinned version.
See the [uv tool guide](https://docs.astral.sh/uv/guides/tools/) for details.

## Create a starter policy

From a repository that does not already have a discovered policy:

```bash
yaga config init --repo .
yaga config show --repo .
```

Initialization creates only `.yaga.toml` and refuses to overwrite an existing discovered policy.
Policy discovery selects one nearest `.yaga.toml` or `pyproject.toml`; use `--config` when the
source must be explicit. Use `yaga config init --repo . --dry-run` when a bootstrap or CI
step should validate and inspect the starter policy without writing it.

## Check a commit

With no source option, `commit check` checks `HEAD`. Otherwise select exactly one source:

```bash
yaga commit check
yaga commit check --message "feat(cli): add a check

Explain the new check with enough context for future maintainers."
yaga commit check --file .git/COMMIT_EDITMSG
yaga commit check --range origin/main..HEAD
```

This complete-message example also passes YAGA's own stricter checkout policy, which requires
a prose body. Other repositories can keep the starter policy's optional body.

The stable exit contract is:

| Exit | Meaning |
| --- | --- |
| `0` | All checked records passed. |
| `1` | A policy finding was reported. |
| `2` | Input, configuration, repository, or Git operation failed. |

Use `--format json` for a versioned machine-readable report and `--format github` for escaped
workflow annotations.

## Add the commit-message hook

YAGA exposes one direct `commit-msg` adapter through `.pre-commit-hooks.yaml`:

```yaml
repos:
  - repo: https://github.com/dariuszpanas/yaga
    rev: <audited-40-character-commit-sha>
    hooks:
      - id: yaga-commit-check
```

Install that hook type explicitly:

```bash
pre-commit install --hook-type commit-msg --install-hooks
```

The hook complements CI; it cannot inspect merge parents or replace a complete-history range
check.

## Install from a checkout

Source installation is for contributors and users who want unreleased code. For normal use,
choose a PyPI installation above.

```bash
git clone https://github.com/dariuszpanas/yaga.git
cd yaga
```

Checkout commands require the exact uv version declared by `required-version` in
`pyproject.toml`. If uv reports a mismatch, update it through your installation method to the
version shown in that error.

To install the current source as a user-wide tool:

```bash
uv tool install --python 3.12 .
uv tool update-shell
```

This includes local source edits. Select a reviewed commit before installing if you need an exact
revision. Later source edits do not change the installed tool; rerun
`uv tool install --python 3.12 --reinstall .` from the checkout after updating it.

For development, tests, and documentation, use the checkout environment instead:

```bash
uv sync --frozen --group dev
uv run yaga --help
```

This creates the checkout's `.venv`. Use `uv run yaga` for that environment; use `yaga` directly
for the global tool. See [Contributing](contributing.md) for the development checks.

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

For the complete operating model, see [Usage modes](usage.md) and [Configuration](configuration.md).
