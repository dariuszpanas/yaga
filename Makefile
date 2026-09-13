.PHONY: actionlint build check docs docs-serve test ci

DOCS_ADDR ?= localhost:9000
BUILD_ARGS ?=

actionlint:
	uv run yaga workflow lint .github/workflows examples

docs:
	uv run zensical build --strict --clean

docs-serve:
	uv run zensical serve --dev-addr "$(DOCS_ADDR)"

build:
	uv run python scripts/check_build.py $(BUILD_ARGS)

check:
	uv lock --check
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check
	uv run pre-commit validate-manifest .pre-commit-hooks.yaml
	uv run yaga repo check --plan .yaga/checks/ci.toml --commit HEAD --revision HEAD
	uv run yaga change check --policy .yaga/change-policy.toml --range HEAD^..HEAD
	bash -n scripts/composite_smoke_python

test:
	uv run pytest

ci: check test build docs
