# Using YAGA from Rust and Python

Build the standalone executable with Cargo from the repository root.

```console
cargo build --release --locked -p yaga-cli
cargo run --locked -p yaga-cli -- branch check --policy .yaga/branch-policy.toml --name feat/native-rust
```

The executable is `target/release/yaga` on Unix and `target/release/yaga.exe` on Windows. Cargo builds the standalone executable and library without a Python interpreter. Git-backed commands need a supported Git installation. Spelling checks and workflow lint use their existing external tools.

## Rust library

See [Use yaga-core in a Rust project](../docs/getting-started.md#use-yaga-core-in-a-rust-project)
for a Cargo path dependency and a runnable branch-policy example.

Run `cargo doc --locked -p yaga-core --open` for the Rust modules and their public signatures.

The Rust library and standalone CLI accept TOML 1.1, including TOML 1.0 configuration files.

## Python package

Platform wheels (`py3-none-<platform>`) bundle the standalone `yaga` Rust executable alongside a zero-dependency Python launcher (`src/yaga/`). Both `yaga` and `python -m yaga` locate and execute the bundled Rust binary directly, and `yaga.find_yaga_bin()` returns its path.

Source and editable installations build the standalone Rust executable with Cargo and `rustc`. Run `uv run make native-wheel` to write a platform wheel in `dist/`. Reinstall after changing Rust sources. For `uv tool install yaga-cli` installations, the launcher handles `yaga self update` by verifying `uv` tool ownership and delegating to `uv tool upgrade yaga-cli`.

## Native installation management

The shell and PowerShell installers select a verified native executable for a supported
release and record its origin and digest. Use `yaga self update --dry-run` to inspect an
update. A release installation verifies the manifest and executable before replacing the
installed binary. On Windows the updater waits for the running executable to exit and
records the outcome in the log it prints.

For development, [install the Rust CLI with Cargo](../docs/getting-started.md#install-the-rust-cli-with-cargo).
Update the checkout to the intended revision before running `yaga self update`. Cargo
installs rebuild with their recorded features, build profile and target. Update a manually
copied binary through its original source. Python tool installations use their existing `uv`
update flow.

## Qualification and testing

Run `cargo fmt --all --check`, `cargo clippy --workspace --all-targets --locked -- -D warnings`, and `cargo test --workspace --locked` to verify the Rust library and CLI directly. Run `uv run make ci` for full repository, wheel packaging, documentation, and launcher checks (`scripts/check_native.py` qualifies the Rust workspace, an independent `yaga-core` consumer crate, isolated `py3-none` wheel installation, and the packaging/installer test suite).

The standalone CLI includes commit, branch, change, repository, tree, workflow and pull-request checks. It also supports test planning and receipt evaluation, review gates, policy initialization and shell completion. Use `yaga --help` and the corresponding subcommand help to inspect the options. The composite Action invokes the Rust executable for commit checks, other providers and trusted gate operations. Only the gate and explicitly live provider operations receive the credentials they require.
