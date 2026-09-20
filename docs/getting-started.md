# Get started

This guide covers **YAGA 0.1.2 (Beta)**. YAGA requires Python 3.12 or newer. Install the **`yaga-cli`** distribution from PyPI;
the executable and import package are **`yaga`**. The `yaga` distribution is an unrelated project.

## Install as a global tool

A tool installer makes `yaga` available from any repository without adding it to each project's
dependencies. Choose one installation method below.

### Convenience installer

The scripts bootstrap uv when it is absent, then install the `yaga-cli` PyPI distribution in a
per-user tool environment with Python 3.12. They do not require administrator access and do not
install Git, Docker, Typos, or optional model dependencies.

On macOS or Linux:

```bash
curl -fsSL https://dariuszpanas.github.io/yaga/install.sh | sh
```

Or use wget:

```bash
wget -qO- https://dariuszpanas.github.io/yaga/install.sh | sh
```

The script reuses an existing uv installation. When uv needs to be installed, it prefers curl
and falls back to wget if curl is unavailable. If neither downloader is available, it stops
with instructions. A download failure stops installation rather than trying another downloader.

In PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://dariuszpanas.github.io/yaga/install.ps1 | iex"
```

`-ExecutionPolicy Bypass` applies to this PowerShell process; it does not persistently change your
execution policy. This follows [uv's Windows installation instructions](https://docs.astral.sh/uv/getting-started/installation/#standalone-installer).

You can [inspect the shell script](install.sh) or [PowerShell script](install.ps1), download it,
and execute the saved file instead. These convenience commands execute downloaded code.
The bootstrap uses uv 0.12.7; an existing uv is reused. Set `YAGA_VERSION` to a published final
`X.Y.Z` release to pin YAGA, or omit it for the latest release. Set `YAGA_NO_MODIFY_PATH=1` to
skip `uv tool update-shell`; then add the directory reported by `uv tool dir --bin` to `PATH`
yourself. Standard uv environment variables, including `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR`,
remain supported. A failed download or install stops the script; it does not report success.

Run the installer once, then use the update command below. An existing installation is managed
by uv's normal conflict handling; the script does not force replacement of another executable.

### MSYS2, Git Bash, and Cygwin on Windows

Use the shell installer above. Native Windows uv manages Windows' `PATH`, which may differ from
your POSIX shell's `PATH`. The shell installer prints an export command with the resolved tool
directory and the selected uv directory instead of invoking `uv tool update-shell` on these platforms.
This also makes a freshly bootstrapped uv available. If uv is already on your PATH, you can resolve
the directory yourself:

```bash
export PATH="$(cygpath -u "$(uv --no-config tool dir --bin)"):$PATH"
hash -r
yaga --help
```

Run them in your current shell; a piped installer cannot change its parent shell's environment.
Add the export line to `~/.zshrc` for zsh or `~/.bashrc` for Bash to persist it. The installer
does not edit these files. This also applies when installing with uv directly. uv itself must
be on your shell's `PATH`.

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

### Update or uninstall

For a uv tool installation (including the convenience installer), run:

```bash
yaga self update --dry-run
yaga self update
```

YAGA verifies that uv owns its current environment and delegates to `uv tool upgrade yaga-cli`.
uv retains the stored version constraint and extras, so an exact pin remains pinned. To change
that constraint, use `uv tool install --python 3.12 "yaga-cli==X.Y.Z"` with the new published
version. Updating is explicit and requires network access; ordinary checks never update YAGA.
Project environments and pipx installations receive manager-specific instructions instead of
being modified by `self update`.

On Windows, the running `yaga.exe` is locked. YAGA starts a hidden worker using the base Python
interpreter, exits, and lets that worker run uv after the launcher closes. The command prints a
temporary completion-log path and copyable PowerShell and Bash/zsh commands to read it;
scheduling is not proof of success. Repeat the log command until the completion code appears. Wait for
`YAGA_UPDATE_EXIT_CODE=0` in the log, then run `yaga --version`. A different code means the update
failed: inspect the log and run `uv tool upgrade yaga-cli` directly to retry. The helper removes
itself and retains the log for inspection. On other platforms the command waits for uv to finish.
For foreground progress on Windows, run `uv tool upgrade yaga-cli` directly instead of starting
YAGA; uv can then replace the executable without waiting for a handoff. This works in PowerShell,
MSYS2, and Git Bash.

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

## Add checks to your workflow

Once a local check works, follow the [adoption recipes](recipes.md) for hooks and CI.
See [commit policy](commit-policy.md) for the full message and hook reference.

## Install from a checkout

Source installation is for contributors and users who want unreleased code. For normal use,
choose a PyPI installation above.

```bash
git clone https://github.com/dariuszpanas/yaga.git
cd yaga
```

Checkout commands require uv 0.12.7 or newer. CI pins its own version for reproducibility.
See [Contributing](contributing.md) for the development tool prerequisites.

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

For the complete operating model, see [Usage modes](usage.md) and [Configuration](configuration.md).
