# yaga-core

Deterministic Git, commit, pull-request, workflow, test-planning, and repository policy checks for Rust callers.

`yaga-core` is the standalone policy engine behind [YAGA](https://dariuszpanas.github.io/yaga/) (`yaga-cli`). It reads the exact same versioned TOML policies as the CLI and GitHub Action with zero Python dependency, bounded inputs and matcher work, and shell-free Git inspection.

## Installation

Install the published `0.6` release from crates.io:

```toml
[dependencies]
yaga-core = "0.6"
```

On unreleased `main` (planned for `0.7`; not present in published `0.6.0`), pure in-memory policy
evaluation (without filesystem, subprocess, or OS-FFI dependencies) disables the default `process`
feature and exposes byte-slice policy auditors (`policies::classify_bytes`,
`policies::audit_bytes`, `policies::audit_entries`) plus non-filesystem plan helpers
(`RepositoryPlan::relative_policy_paths`, `RepositoryPlan::relative_workflow_paths`):

```toml
[dependencies]
# Unreleased `main` (planned for 0.7; not available in published 0.6.0):
yaga-core = { git = "https://github.com/dariuszpanas/yaga", default-features = false }
```

Cargo features:

- `process` (enabled by default on `main` / upcoming `0.7`; always enabled in published `0.6.0`): filesystem helpers, shell-free `git`/`gh`/`typos`/`docker` runners, policy discovery/init, and shell completions
- `github-http`: bounded GitHub API HTTP transport (`ureq` with `rustls`; implies `process`)
- `quality-http`: hosted OpenAI / Anthropic / Gemini / OpenAI-compatible commit quality clients (implies `process`)
- `quality-bedrock`: Amazon Bedrock Converse commit quality client (implies `process`)

## Example

```rust,no_run
use std::path::Path;
use yaga_core::branch::{BranchError, check_branch};

fn main() -> Result<(), BranchError> {
    let checked = check_branch(Path::new(".yaga/branch-policy.toml"), "feat/example")?;
    println!("valid = {}", checked.report.valid());
    Ok(())
}
```

## Documentation

- [API documentation (docs.rs)](https://docs.rs/yaga-core)
- [YAGA documentation](https://dariuszpanas.github.io/yaga/)
- [CLI package (`yaga-cli`)](https://crates.io/crates/yaga-cli)
- [Changelog](https://dariuszpanas.github.io/yaga/changelog.html)

Licensed under BSD-3-Clause.
