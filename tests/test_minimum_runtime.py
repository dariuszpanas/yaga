"""Lowest-runtime evidence must exclude ambient environments and checkout imports."""

from pathlib import Path

import pytest

from scripts import check_minimum_runtime


def test_installer_and_python_overrides_do_not_change_lowest_runtime() -> None:
    environment = check_minimum_runtime.isolated_environment(
        {
            "PATH": "tools",
            "SYSTEMROOT": "system",
            "VIRTUAL_ENV": "checkout/.venv",
            "PYTHONPATH": "checkout/src",
            "PYTHONHOME": "another-python",
            "UV_RESOLUTION": "highest",
            "UV_CONSTRAINT": "constraints.txt",
            "PIP_INDEX_URL": "https://other.invalid/simple",
            "YAGA_ACTION_RUNTIME": "1",
            "YAGA_COMMIT_ACTION_RUNTIME": "1",
        }
    )
    assert environment == {
        "PATH": "tools",
        "SYSTEMROOT": "system",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }


def inventory(environment: Path) -> dict[str, object]:
    return {
        "prefix": str(environment.resolve()),
        "imports": {
            name: str(environment / "site-packages" / name / "__init__.py")
            for name in check_minimum_runtime.IMPORTED_MODULES
        },
        "distributions": {"yaga-cli": "0.0.0", "typer": "0.27.2", "rich": "13.8.0"},
    }


def test_inventory_accepts_only_the_new_environment(tmp_path: Path) -> None:
    document = inventory(tmp_path / "runtime")
    check_minimum_runtime.validate_inventory(document, tmp_path / "runtime")
    with pytest.raises(SystemExit, match="wrong Python environment"):
        check_minimum_runtime.validate_inventory(document, tmp_path / "another-runtime")


@pytest.mark.parametrize("source", ["checkout", "other-environment"])
def test_inventory_rejects_leaking_imports(tmp_path: Path, source: str) -> None:
    document = inventory(tmp_path / "runtime")
    imports = document["imports"]
    assert isinstance(imports, dict)
    imports["yaga.cli"] = str(
        check_minimum_runtime.ROOT / "src/yaga/cli.py"
        if source == "checkout"
        else tmp_path / "other-environment/yaga/cli.py"
    )
    with pytest.raises(SystemExit, match="outside its installed environment"):
        check_minimum_runtime.validate_inventory(document, tmp_path / "runtime")


def test_inventory_requires_cli_and_dependency_provenance(tmp_path: Path) -> None:
    document = inventory(tmp_path / "runtime")
    imports = document["imports"]
    assert isinstance(imports, dict)
    del imports["typer"]
    with pytest.raises(SystemExit, match="missing expected imports"):
        check_minimum_runtime.validate_inventory(document, tmp_path / "runtime")
