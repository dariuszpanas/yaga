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

The Rust library and standalone CLI accept TOML 1.1, including TOML 1.0 configuration files. Python installations use the running interpreter’s TOML parser, which supports 1.0 on CPython 3.12–3.14 and 1.1 starting with 3.15. Malformed TOML can produce different parser messages and reported columns.

## Python package

Platform wheels install the Rust CLI and the CPython stable-ABI extension together. Both `yaga` and `python -m yaga` run the packaged executable for ordinary commands, including Git ranges and project configuration discovery. Python applications can keep using the existing import paths. The stable ABI starts at CPython 3.12.

Source and editable installations build the Rust executable and extension with Cargo and rustc. Run `uv run make native-wheel` to write a platform wheel in `dist/`. Editable installations load live Python sources and keep the compiled files in the installation. Reinstall after changing Rust sources. The wheel uses the running Python installation to locate its manager for self-updates. Rust validates the update arguments and proves uv ownership before requesting a dry run or upgrade. Python preserves the interpreter's process streams and the Windows launcher handoff. The wheel also preserves the active shell's completion protocol. Interpreter-specific Unicode and TOML inputs use the running Python version's semantics.

The local Linux wheel targets the build host's Linux ABI. The local Windows wheel targets Windows x64. The Python package uses the running interpreter's Unicode rules for commit and description checks.

## Native installation management

The shell and PowerShell installers select a verified native executable for a supported
release and record its origin and digest. Use `yaga self update --dry-run` to inspect an
update. A release installation verifies the manifest and executable before replacing the
installed binary. On Windows the updater waits for the running executable to exit and
records the outcome in the log it prints.

For development, [install the Rust CLI with Cargo](../docs/getting-started.md#install-the-rust-cli-with-cargo).
Update the checkout to the intended revision before running `yaga self update`. Cargo
installs rebuild with their recorded features, build profile and target. Update a manually
copied binary through its original source. Python tool installations use their existing uv
update flow.

## Checking compatibility

The differential suite compares the current CLI and wheel with the pre-Rust command installed in a separate Python environment. Set `YAGA_TEST_REFERENCE_PYTHON` to use a prepared reference installation.

Run `uv run make ci` for the existing Python, repository and build checks. Run `uv run make native-ci` for Rust formatting, lint, tests, isolated wheel installation and the Python suite against the installed extension. That suite also compares standalone CLI output with the original Python implementation and exercises privileged gate operations against an in-memory provider. Run both from the repository root. `native-ci` needs Cargo, Rustfmt, Clippy, CPython 3.12, uv, Git, Node.js and Docker. CI also installs Rust 1.100.0 to exercise the unpublished Action's source build.

The standalone CLI includes commit, branch, change, repository, tree, workflow and pull-request checks. It also supports test planning and receipt evaluation, review gates, policy initialization and shell completion. Use `yaga --help` and the corresponding subcommand help to inspect the options. The composite Action invokes the Rust executable for commit checks, other providers and trusted gate operations. Only the gate and explicitly live provider operations receive the credentials they require.
