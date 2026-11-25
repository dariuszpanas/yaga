# yaga-core

Deterministic Git, commit, pull-request, workflow, test-planning, and repository policy checks for Rust callers.

`yaga-core` is the standalone policy engine behind [YAGA](https://dariuszpanas.github.io/yaga/) (`yaga-cli`). It reads the exact same versioned TOML policies as the CLI and GitHub Action with zero Python dependency, bounded inputs and matcher work, and shell-free Git inspection.

## Installation

```toml
[dependencies]
yaga-core = "0.6"
```

For pure in-memory policy evaluation (without filesystem, subprocess, or OS-FFI dependencies):

```toml
[dependencies]
yaga-core = { version = "0.6", default-features = false }
```

Cargo features:

- `process` (enabled by default): filesystem helpers, shell-free `git`/`gh`/`typos`/`docker` runners, policy discovery/init, and shell completions
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
