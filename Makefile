.PHONY: actionlint build check docs docs-serve test ci
.PHONY: native-ci native-wheel

DOCS_ADDR ?= localhost:9000
BUILD_ARGS ?=
CHANGE_RANGE ?= HEAD^..HEAD

actionlint:
	uv run --frozen yaga workflow lint .github/workflows examples

docs:
	uv run --frozen zensical build --strict --clean
	uv run --frozen python scripts/check_docs.py
	uv run --frozen python scripts/check_description.py

docs-serve:
	uv run --frozen zensical serve --dev-addr "$(DOCS_ADDR)"

build:
	uv run --frozen python scripts/check_build.py $(BUILD_ARGS)

check:
	cargo fmt --all --check
	cargo clippy --workspace --all-targets --locked -- -D warnings
	cargo clippy -p yaga-core --no-default-features --all-targets --locked -- -D warnings
	uv lock --check
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .
	uv run --frozen ty check
	uv run --frozen pre-commit validate-manifest .pre-commit-hooks.yaml
	uv run --frozen yaga policy check --strict
	uv run --frozen yaga repo check --plan .yaga/checks/ci.toml --commit HEAD --revision HEAD
	@if [ -n "$(CHANGE_RANGE)" ]; then uv run --frozen yaga change check --policy .yaga/change-policy.toml --range "$(CHANGE_RANGE)"; fi

test:
	cargo test --workspace --locked
	cargo test -p yaga-core --no-default-features --locked
	uv run --frozen pytest

ci: check test build docs

native-ci:
	uv run --frozen --python 3.12 python scripts/check_native.py

native-wheel:
	uv sync --frozen --group dev --no-install-project --python 3.12
	uv build --wheel --no-build-isolation --python .venv
