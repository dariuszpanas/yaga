# YAGA repository agent guidance

Read `CONTRIBUTING.md` before changing code or opening a pull request. Start with
`git status --short`, `git branch --show-current`, and `git log -1 --oneline`; preserve
unrelated work, stage explicit paths, and inspect both working and cached diffs. Feature
branches use `feat/<topic>` and fixes use `fix/<topic>`.

## Rust-first architecture

YAGA is a fast, standalone Rust CLI and library (`crates/yaga-core` + `crates/yaga-cli`)
distributed as:

1. **Standalone native executables** (`docs/install.sh`, `docs/install.ps1`, Cargo, and GitHub
   Releases) with zero Python dependency.
2. **Zero-dependency Python platform wheels (`py3-none-<platform>`)** under `src/yaga/` — a thin
   `uv`-style binary launcher (`__init__.py`, `__main__.py`, `entrypoint.py`, `self_update.py`,
   `_update_worker.py`) that locates and executes the bundled `yaga` Rust binary via
   `os.execve` (POSIX) or `subprocess.call` (Windows) and delegates `yaga self update` for
   `uv tool install yaga-cli` installations.
3. **Unified GitHub Action (`action.yml`)** driven by `scripts/action-runtime.cjs` and the
   standalone native Rust binary (`$YAGA_RUNTIME_BINARY __action commit|gate|cli|summary`).

Do **not** reintroduce a PyO3 extension (`crates/yaga-python`), a Python-side duplicate policy
engine, Typer/Pydantic/pydantic-versions runtime dependencies, or Rust-to-Python callback
bridges. All policy parsing, validation, Git inspection, reporting, CLI presentation, and
Action execution belong solely in Rust (`crates/yaga-core` and `crates/yaga-cli`).

## Preserve the CLI and policy contracts

- **CLI look and feel**: `crates/yaga-cli` uses Clap and `fast-rich` (`crates/yaga-cli/src/presentation/`)
  to preserve Typer/Rich-style framed help panels, options tables, color precedence (`NO_COLOR`,
  `FORCE_COLOR`), and framed usage error diagnostics (exit `2`).
- **Exit codes & outputs**: Keep stable exit codes (`0` pass, `1` policy finding or gate
  failure, `2` input/config/operational error), versioned JSON schemas, plain text reports, and
  escaped GitHub workflow annotations across all commands.
- **Minimal, audited `unsafe` Rust**: Keep `#![deny(unsafe_code)]` at the crate root of
  `crates/yaga-core` and prohibit `unsafe` everywhere in policy, parsing, and evaluation logic.
  Narrow `#[allow(unsafe_code)]` exceptions with explicit `// SAFETY:` comments are permitted
  only at irreducible OS-FFI boundaries (`git/unix.rs` `pre_exec`/`setsid`, `git/windows.rs`
  Win32 Job Object and thread handles, and `fs/user_path.rs` POSIX `getpwuid_r`/`getpwnam_r`).
- **Untrusted input bounds**: Treat commit messages, PR bodies, Git output, revisions, paths,
  YAML/TOML inputs, and displayed labels as untrusted. Bound inputs, output, matcher work
  (`10,000,000` units), and timeouts; invoke external tools (`git`, `gh`, `typos`, `docker`,
  `gpgv`) without a shell; reject option-like/split revisions and legacy graft overlays; and
  sanitize terminal text.
- **Commit policy (`crates/yaga-core/src/commits/`, `github_commit/`)**:
  - `commit check` accepts one explicit source or defaults to `HEAD`; never silently combine
    sources, fetch history, or ignore a missing base.
  - Preserve schema-v1 configuration rules (`allowed-types`, `scope-policy`,
    `scope-policy-by-type`, `breaking-markers`, `body-policy`, `body-min-words`,
    `required-footer-tokens`, `forbidden-footer-tokens`, `typos`, `dependabot-pull-requests`,
    `dependabot-require-github-signature`, `skip-pull-request-authors`, `merge-commits`,
    `max-commits`).
  - Commit quality (`commit quality`) uses explicit hosted/OpenAI-compatible providers
    (`openai`, `anthropic`, `gemini`, `bedrock`, `openai-compatible`); bundled local inference
    is retired with a migration error (exit `2`) while old configs remain readable for ordinary
    commit checks.
- **Pull-request checks (`descriptions/`, `review/`, `triage/`, `agent_review/`)**:
  - Keep `pull-request description check`, `pull-request review check`, and
    `pull-request triage check` offline for `--file`/`--stdin`/`--event-file` and explicitly
    bounded via `gh` for `--pr`.
  - Keep PR trust triage isolated and fail-closed: require explicit current-head maintainer
    admission before automated positive review publication; never treat PR instructions,
    labels, bot reviews, or quota markers as maintainer approval.
  - Keep `gate agent-review` isolated under `crates/yaga-core/src/agent_review/`.
- **Committed-tree & path checks (`paths/`, `branch/`, `git/`)**:
  - Keep `branch check`, `change check`, `mode check`, `path check`, `size check`, and
    `tree check` explicit and read-only on committed Git objects (never inspecting working-tree,
    index, or untracked state, and never fetching or recursing into gitlinks).
- **Workflow & repository checks (`workflows/`, `actionlint/`, `repository/`)**:
  - Keep `workflow check` (immutable SHA `uses` and container digests), `workflow security`
    (`recommended-v1` default, opt-in `recommended-v2` and `recommended-v3`), and
    `workflow lint` (pinned `actionlint` container) distinct without merging diagnostics.
  - Keep `repo check` and `repo inspect` explicit with canonical provider order and closed
    portable plan v1/v2 schemas.
- **Test planning (`test_planning/`, `policies/testing.rs`)**:
  - Keep `test policy check`, `test plan`, and `test evaluate` pure, deterministic, and
    bounded with canonical digests and exact identity verification.
- **Policy management & shell completion (`policies/`, `completion.rs`)**:
  - Keep `policy init` (interactive selector, `--all`, `--skip-existing`, `--dry-run`) and
    `policy check` (`--strict`, `required-version`) covering all 12 versioned TOML families.
  - Keep BOM-safe PowerShell/bash/zsh/fish completion scripts without calling
    `Set-ExecutionPolicy`.
- **`.pre-commit-hooks.yaml`**: Keep as one direct Python `commit-msg` adapter invoking
  `yaga commit check --file`.

## Testing & verification

- Primary unit and integration tests live in Rust (`crates/yaga-core` and `crates/yaga-cli/tests/`),
  verified with `cargo fmt --all --check`, `cargo clippy --workspace --all-targets --locked -- -D warnings`,
  and `cargo test --workspace --locked`.
- Python tests under `tests/` cover only the thin launcher (`src/yaga/`), wheel/release
  packaging scripts, standalone installers, docs links, and Action fixture controls.
- Keep `Cargo.lock` and `uv.lock` synchronized whenever dependencies change, and ensure commits
  satisfy `.yaga/` policies (Conventional Commits with required scope for `feat`/`fix`,
  `>= 8`-word body, paired `!` + `BREAKING CHANGE:` footer, `<= 1000` commits per PR).
