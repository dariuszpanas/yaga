# Minimum runtime qualification

The `yaga-cli` Python package is a zero-dependency `py3-none-<platform>` binary launcher for the
bundled Rust `yaga` executable. Because `pyproject.toml` declares `dependencies = []`, installing
`yaga-cli` via `pip`, `pipx`, `uvx`, or `uv tool install` pulls **no** third-party Python runtime
packages.

## Verification

Every wheel build check (`scripts/check_build.py` and `scripts/release_wheels.py`) enforces this
zero-dependency contract:

- Rejects any wheel metadata containing `Requires-Dist` runtime dependencies.
- Installs the wheel with `--no-deps` into an isolated virtual environment outside the checkout.
- Verifies that `importlib.metadata` reports only `yaga-cli` in that environment and that
  `yaga.find_yaga_bin()` resolves to the packaged executable inside `sys.prefix`.
- Runs `yaga --version`, `python -m yaga --version`, and core CLI checks (`commit`, `branch`,
  `policy`) end-to-end.

## Security audit of Rust dependencies

All policy parsing, validation, Git inspection, and HTTP/Bedrock clients live in the Rust workspace
(`Cargo.toml` / `Cargo.lock`) and are built with `--locked` across CI and release workflows.
