.PHONY: actionlint build check test ci

actionlint:
	uv run yaga workflow lint .github/workflows examples

build:
	uv run python scripts/check_build.py

check:
	uv lock --check
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check
	uv run pre-commit validate-manifest .pre-commit-hooks.yaml
	uv run yaga repo check --check commit --check workflow --check workflow-security --check workflow-lint --commit HEAD --workflow-path .github/workflows --workflow-path examples
	bash -n scripts/composite_smoke_python

test:
	uv run pytest

ci: check test build
