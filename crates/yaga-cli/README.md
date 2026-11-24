# yaga-cli

**Repository conventions you can check locally and enforce in CI.**

`yaga-cli` installs the standalone `yaga` executable for deterministic commit, pull-request, branch, changed-path, committed-tree, workflow, test-planning, and repository policy checks — with zero Python dependency and stable exit codes (`0` pass, `1` finding, `2` input or operational error).

## Installation

### Cargo (crates.io)

```bash
cargo install --locked yaga-cli
yaga --version
```

### Standalone installers (no Rust or Python required)

Linux / macOS:

```bash
curl -fsSL https://dariuszpanas.github.io/yaga/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://dariuszpanas.github.io/yaga/install.ps1 | iex
```

### Python launcher (`uv` / `pipx` / `pip`)

```bash
uv tool install yaga-cli
```

## Quick start

```bash
yaga policy init --repo .
yaga policy check --repo . --strict
yaga commit check
```

## Documentation

- [Get started](https://dariuszpanas.github.io/yaga/getting-started.html)
- [Command reference](https://dariuszpanas.github.io/yaga/commands.html)
- [Configuration](https://dariuszpanas.github.io/yaga/configuration.html)
- [GitHub Actions (`YAGA Checks`)](https://dariuszpanas.github.io/yaga/github-actions.html)
- [Rust library (`yaga-core`)](https://crates.io/crates/yaga-core)
- [Changelog](https://dariuszpanas.github.io/yaga/changelog.html)

Licensed under BSD-3-Clause.
