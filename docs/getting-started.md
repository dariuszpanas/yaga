# Get started

Use the standalone `yaga` executable, install `yaga-cli` with Cargo from crates.io, or install
the zero-dependency `yaga-cli` Python package from PyPI. All three ship the same Rust CLI, read
the same policy files, and support the same report formats. The PyPI and crates.io packages named
`yaga` are unrelated.

## Install as a global tool

### Standalone installer

On Linux and macOS:

```bash
curl -fsSL https://dariuszpanas.github.io/yaga/install.sh | sh
```

With GNU Wget:

```bash
wget -qO- https://dariuszpanas.github.io/yaga/install.sh | sh
```

On Windows:

```powershell
irm https://dariuszpanas.github.io/yaga/install.ps1 | iex
```

The standalone release covers x86-64 and ARM64 on Linux, macOS and Windows. Linux binaries are
statically linked with musl. Git-backed commands need a supported Git, spelling checks need
Typos, and workflow lint needs Docker. For optional model-backed feedback, see
[commit quality providers](commit-quality.md).

Inspect the [shell installer](install.sh) or [PowerShell installer](install.ps1) to see what
runs. To pin a version, download the script and run `sh install.sh --version X.Y.Z` or
`.\install.ps1 -Version X.Y.Z`. With a piped installer, set `YAGA_VERSION` first. An unpinned
install selects the latest release. The installer verifies the versioned SHA-256 manifest, the
binary checksum and the executable's reported version before replacing a previous installation.
It preserves executables owned by other package managers. Choose a separate install directory or
remove a previous installation with its own manager before switching.

The default installation directory is `~/.local/bin` on all three systems. Pass
`--install-dir <absolute-path>` (`-InstallDir` in PowerShell) or set `YAGA_INSTALL_DIR` to change it.
On Unix the installer adds the directory to an existing shell's startup configuration for bash,
zsh or sh if needed. In MSYS2, Git Bash or Cygwin it prints the export to run. PowerShell
updates your per-user PATH. If the shell profile or per-user PATH cannot be updated, the
installer prints a command that makes the installed executable available in the current shell.
Set `YAGA_NO_MODIFY_PATH=1` to manage PATH yourself.

On a Unix runner without a home directory, pass an absolute install directory. The installer
prints a PATH command you can use in the job. Zsh also honors an explicit `ZDOTDIR`.

Use `--python` (`-Python` in PowerShell) or `YAGA_INSTALL_METHOD=python` for a per-user uv tool
environment. This path reuses uv or bootstraps uv 0.12.7 and selects Python 3.12. Published
versions through 0.5.0 can use this path automatically when their native manifest is absent.
Pass `--native` or set `YAGA_INSTALL_METHOD=native` to require a standalone release. An invalid
checksum, missing binary or unsuccessful download always stops a native install.

Private mirrors may set `YAGA_RELEASE_BASE_URL` to their trusted HTTPS releases base. The installer
reads `latest/download/yaga-native-sha256.txt` or `download/vX.Y.Z/yaga-native-sha256.txt` and
fetches the named executable under the exact version. A loopback fixture may use
`http://127.0.0.1:PORT`. Only an explicitly selected loopback origin permits HTTP. Installation
trusts the selected origin for both the manifest and the binary.

### Cargo (crates.io)

With a Rust toolchain (MSRV 1.99+), install the standalone `yaga` executable from crates.io:

```bash
cargo install --locked yaga-cli
yaga --version
```

Cargo installs `yaga` in its bin directory (by default `~/.cargo/bin`) with no Python dependency.
Update later with `cargo install --locked yaga-cli` or remove it with `cargo uninstall yaga-cli`.

### Python with uv

[Install uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
uv tool install --python 3.12 yaga-cli
uv tool update-shell
```

Open a new terminal after updating PATH. Use `uv tool dir --bin` to locate your tool directory.
The Python launcher supports Python 3.12 through 3.15 with zero runtime dependencies and bundles
the standalone Rust `yaga` executable. Standard uv settings, including `UV_TOOL_DIR` and
`UV_TOOL_BIN_DIR`, apply to Python installations.

### pipx

With [pipx](https://pipx.pypa.io/stable/how-to/install-pipx.html) and Python 3.12 or newer installed:

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

Use `yaga self update --dry-run` to check which installation would be updated. For a standalone
installation, `yaga self update` verifies the local receipt and installs the latest release from
the configured source. Set `YAGA_RELEASE_BASE_URL` again when updating from a private mirror.
On Windows the command prints a completion log and exits so the installer can replace the running
executable. A successful update records `YAGA_UPDATE_EXIT_CODE=0` in that log.

To uninstall, delete the installed `yaga` (Windows `yaga.exe`) and the adjacent
`.yaga-native-receipt`. Repositories and policy files are independent of these files.

For a uv tool installation, run:

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
| Standalone installer | `yaga self update` | Delete `yaga` (`yaga.exe`) and `.yaga-native-receipt` |
| Cargo | `cargo install --locked yaga-cli` | `cargo uninstall yaga-cli` |
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

To scaffold multiple starter policies under `.yaga/` (branch, change, description, repository plan,
committed-tree, agent-review, or test policies) and audit every policy file in your repository
against YAGA's versioned schemas:

```bash
yaga policy init --repo .
yaga policy check --repo . --strict
```

In an interactive terminal with no policy names or `--all`, `yaga policy init` opens a checkbox
selector; in scripts or CI, pass explicit policy names or `--all` (plus `--skip-existing` or
`--dry-run` as needed). See the [command reference](commands.md) for all 12 supported policy kinds.

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

Once a local check works, follow the [adoption recipes](recipes.md) for hooks and CI, or wire
the unified [GitHub Action (`YAGA Checks`)](github-actions.md) (`uses: dariuszpanas/yaga@...`)
into your pull-request workflows. See [commit policy](commit-policy.md) for the full message and
hook reference.

## Install from a checkout

Start with a YAGA checkout at a revision you have reviewed. The examples below assume
the checkout directory is named `yaga`.

### Install the Rust CLI with Cargo

From the root of the YAGA checkout, install the standalone executable with the dependency
versions in `Cargo.lock`. Cargo builds this CLI without Python or uv.

```bash
cargo install --locked --path crates/yaga-cli
yaga --version
```

Cargo installs `yaga` in its bin directory (by default `~/.cargo/bin`). Include that directory
on your `PATH` to use the installed command.

### Use yaga-core in a Rust project

Add the published `0.6` release of `yaga-core` from crates.io to your project's `Cargo.toml`:

```toml
[dependencies]
yaga-core = "0.6"
```

On unreleased `main` (planned for `0.7`; not available in published `0.6.0`), pure in-memory
consumers (such as virtual filesystems, object stores, or sandboxed services) that do not need
filesystem loaders or subprocess runners can disable the default `process` feature via a pinned
`git` dependency:

```toml
[dependencies]
# Unreleased `main` (planned for 0.7; not present in published 0.6.0):
yaga-core = { git = "https://github.com/dariuszpanas/yaga", default-features = false }
```

Pure `main` (`0.7`) builds omit all process, filesystem, and OS-FFI crates while keeping the
in-memory parsers and evaluators (`commits`, `branch`, `descriptions`, `paths`, `workflows`,
`test_planning`, `triage`, `review`, `agent_review`), plus the unreleased byte-slice policy
auditing (`policies::classify_bytes`, `policies::audit_bytes`, `policies::audit_entries`) and
non-filesystem plan helpers (`RepositoryPlan::relative_policy_paths`,
`RepositoryPlan::relative_workflow_paths`).

Or, against a local reviewed `yaga/` checkout next to your project, use a path dependency:

```toml
[dependencies]
yaga-core = { path = "../yaga/crates/yaga-core" }
```

This `src/main.rs` checks a branch against the same TOML policy that `yaga branch check`
reads. Run `cargo run` from your project's root to use the example policy in the YAGA
checkout. To check your project's rules, set `policy` to the path of its TOML file.

```rust
use std::path::Path;
use yaga_core::branch::{BranchError, check_branch};

fn main() -> Result<(), BranchError> {
    let policy = Path::new("../yaga/.yaga/branch-policy.toml");
    let checked = check_branch(policy, "feat/native-rust")?;
    println!("{}", checked.report.valid());
    Ok(())
}
```

The example prints `true`. A path dependency uses the files in the selected checkout,
including local edits.

### Install the Python package from source

Source and editable installs compile the standalone Rust `yaga` executable with Cargo and rustc
and bundle it into the wheel. Installing a matching release wheel includes the prebuilt binary
and requires no Rust toolchain.

For a user-wide Python tool, run these commands from the YAGA checkout with uv 0.12.7
or newer:

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
