.PHONY: actionlint build check test ci

actionlint:
	uv run python scripts/run_actionlint.py

build:
	uv run python scripts/check_build.py

check:
	uv lock --check
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check
	uv run python scripts/run_actionlint.py
	bash -n scripts/composite_smoke_python

test:
	uv run pytest

ci: check test build
