"""Hatch PEP 517/660 backend for the native distribution."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from hatchling import build as hatch
from hatchling.builders.plugin.interface import IncludedFile
from hatchling.builders.wheel import WheelBuilder


def __getattr__(name: str) -> Any:
    """Delegate sdist, build requirements and optional metadata hooks to Hatch."""
    if name in hatch.__all__:
        return getattr(hatch, name)
    raise AttributeError(name)


class _NativeWheel(WheelBuilder):
    def build_standard(self, directory: str, **build_data: Any) -> str:
        if build_data["pure_python"] or not build_data.get("tag"):
            raise RuntimeError("YAGA requires its native wheel build hook.")
        return super().build_standard(directory, **build_data)


class _EditableWheel(_NativeWheel):
    def recurse_selected_project_files(self) -> Iterator[IncludedFile]:
        # Keep the native files in this installation. Resolve Python modules
        # from the editable source tree, including edits to the package initializer.
        package = Path(self.root, "src", "yaga").resolve()
        with tempfile.TemporaryDirectory() as tmp:
            initializer = Path(tmp, "__init__.py")
            initializer.write_text(
                f"__path__.append({ascii(str(package))})\n"
                f"__file__ = {ascii(str(package / '__init__.py'))}\n"
                "with open(__file__, 'rb') as _source:\n"
                "    exec(compile(_source.read(), __file__, 'exec'))\n",
                encoding="utf-8",
            )
            yield IncludedFile(str(initializer), "", "yaga/__init__.py")


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    builder = _NativeWheel(os.getcwd())
    return Path(next(builder.build(directory=wheel_directory, versions=["standard"]))).name


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    # Hatch's editable target always writes py3-none-any. Use its platform wheel
    # writer and the same required build hook, with only Python sources redirected.
    builder = _EditableWheel(os.getcwd())
    return Path(next(builder.build(directory=wheel_directory, versions=["standard"]))).name
