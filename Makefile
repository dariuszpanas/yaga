.PHONY: actionlint build check docs docs-serve test ci
.PHONY: native-ci native-wheel

DOCS_ADDR ?= localhost:9000
BUILD_ARGS ?=
CHANGE_RANGE ?= HEAD^..HEAD

actionlint:
	uv run yaga workflow lint .github/workflows examples

docs:
	uv run zensical build --strict --clean
	uv run python scripts/check_docs.py

docs-serve:
	uv run zensical serve --dev-addr "$(DOCS_ADDR)"

build:
	uv run python scripts/check_build.py $(BUILD_ARGS)

check:
	cargo fmt --all --check
	cargo clippy --workspace --all-targets --locked -- -D warnings
	uv lock --check
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check
	uv run pre-commit validate-manifest .pre-commit-hooks.yaml
	uv run yaga policy check --strict
	uv run yaga repo check --plan .yaga/checks/ci.toml --commit HEAD --revision HEAD
	uv run yaga change check --policy .yaga/change-policy.toml --range "$(CHANGE_RANGE)"

test:
	cargo test --workspace --locked
	uv run pytest

ci: check test build docs

native-ci:
	uv run --locked --python 3.12 python scripts/check_native.py

native-wheel:
	uv run --locked --python 3.12 uv build --wheel --no-build-isolation
