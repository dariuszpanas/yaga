# yaga-core

Deterministic Git, commit, pull-request, workflow, test-planning, and repository policy checks for Rust callers.

`yaga-core` is the standalone policy engine behind [YAGA](https://dariuszpanas.github.io/yaga/) (`yaga-cli`). It reads the exact same versioned TOML policies as the CLI and GitHub Action with zero Python dependency, bounded inputs and matcher work, and shell-free Git inspection.

## Installation

```toml
[dependencies]
yaga-core = "0.6"
```

Optional Cargo features:

- `github-http`: bounded GitHub API HTTP transport (`ureq` with `rustls`)
- `quality-http`: hosted OpenAI / Anthropic / Gemini / OpenAI-compatible commit quality clients
- `quality-bedrock`: Amazon Bedrock Converse commit quality client

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
