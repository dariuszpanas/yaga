"""Build, qualify, and collect zero-dependency native Python release wheels."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

if __package__:
    from .release_images import (
        check_musl_executable_headers,
        check_pe_image,
        elf_program_headers,
        macos_deployment,
    )
else:
    from release_images import (
        check_musl_executable_headers,
        check_pe_image,
        elf_program_headers,
        macos_deployment,
    )

# Platform tags accepted by pip/uv wheel selection rules.
PLATFORMS = {
    "aarch64-apple-darwin": "macosx_11_0_arm64",
    "x86_64-apple-darwin": "macosx_11_0_x86_64",
    "aarch64-pc-windows-msvc": "win_arm64",
    "x86_64-pc-windows-msvc": "win_amd64",
    "aarch64-unknown-linux-musl": "manylinux_2_28_aarch64",
    "x86_64-unknown-linux-musl": "manylinux_2_28_x86_64",
}


def package_version(source: Path) -> str:
    with (source / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]["version"]


def wheel_name(version: str, tag: str) -> str:
    return f"yaga_cli-{version}-py3-none-{tag}.whl"


def run(*command: str | Path, directory: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=directory, env=env, check=True)


def wheel_payload(wheel: Path, tag: str, scratch: Path) -> Path:
    windows = tag.startswith("win_")
    filename = "yaga.exe" if windows else "yaga"
    member = f"yaga/_bin/{filename}"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if names.count(member) != 1:
            raise ValueError(f"native wheel requires exactly one {member}")
        if any(name.startswith("yaga/_native.") for name in names):
            raise ValueError("native wheel must not bundle a CPython extension module")
        entry = archive.getinfo(member)
        if not 0 < entry.file_size <= 128 * 1024**2:
            raise ValueError(f"invalid native wheel member size: {member}")
        if not windows and not (entry.external_attr >> 16 & stat.S_IXUSR):
            raise ValueError("packaged CLI lacks Unix executable mode")
        (scratch / filename).write_bytes(archive.read(entry))
        metadata = [
            item for item in archive.infolist() if item.filename.endswith(".dist-info/WHEEL")
        ]
        expected = "-".join(wheel.name.split("-")[:2]) + ".dist-info/WHEEL"
        if (
            len(metadata) != 1
            or metadata[0].filename != expected
            or metadata[0].file_size > 64 * 1024
        ):
            raise ValueError("invalid native wheel metadata member")
        headers = Parser().parsestr(archive.read(metadata[0]).decode("ascii"))
        if headers.get_all("Root-Is-Purelib") != ["false"] or headers.get_all("Tag") != [
            f"py3-none-{tag}"
        ]:
            raise ValueError("native wheel metadata does not match its filename")
    executable = scratch / filename
    if windows:
        check_pe(executable, tag, executable=True)
    elif tag.startswith("manylinux"):
        check_elf(executable, tag, executable=True)
    else:
        check_macos(executable, tag, executable=True)
    return executable


def check_pe(path: Path, tag: str, *, executable: bool = True) -> None:
    machine = {"win_arm64": 0xAA64, "win_amd64": 0x8664}[tag]
    check_pe_image(path, machine, executable=executable)


def check_elf(path: Path, tag: str, *, executable: bool = True) -> None:
    """Verify ELF executable architecture and headers (static/static-PIE musl or dynamic)."""
    machine = {"manylinux_2_28_aarch64": 183, "manylinux_2_28_x86_64": 62}[tag]
    with path.open("rb") as stream:
        kind, entry, headers = elf_program_headers(stream, path, machine)
        types = [header[0] for header in headers]
        if 1 not in types:
            raise ValueError(f"{path.name} has the wrong ELF executable or shared-object role")
        if executable:
            # Dynamic glibc executables have PT_LOAD (1) and exactly one PT_INTERP (3).
            # Plain static musl executables have PT_LOAD (1) and no PT_INTERP (3) or PT_DYNAMIC (2).
            if kind in (2, 3) and (types.count(3) == 1 or (2 not in types and 3 not in types)):
                return
            # Static-PIE musl executables (e.g. x86_64-unknown-linux-musl) carry PT_DYNAMIC (2)
            # for self-relocations without PT_INTERP (3) or DT_NEEDED shared libraries.
            try:
                check_musl_executable_headers(stream, kind, entry, headers)
            except ValueError as error:
                raise ValueError(
                    f"{path.name} has the wrong ELF executable or shared-object role"
                ) from error
        else:
            if kind != 3 or 3 in types:
                raise ValueError(f"{path.name} has the wrong ELF executable or shared-object role")


def check_macos(path: Path, tag: str, *, executable: bool = True) -> None:
    """Verify the declared runtime is compatible with the wheel tag."""
    _, major, minor, arch = tag.split("_", 3)
    ceiling = (int(major), int(minor), 0)
    floor = (11, 0, 0) if arch == "arm64" else (10, 0, 0)
    minimum = macos_deployment(path, arch, executable=executable)
    if not floor <= minimum <= ceiling:
        required = ".".join(map(str, minimum))
        raise ValueError(f"{path.name} declares macOS {required}, incompatible with {tag}")


def smoke(wheel: Path, target: str, version: str) -> None:
    tag = PLATFORMS[target]
    if wheel.name != wheel_name(version, tag):
        raise ValueError("expected an exact py3-none platform wheel")
    with tempfile.TemporaryDirectory(prefix="yaga-wheel-consumer-") as temp:
        outside = Path(temp)
        wheel_payload(wheel, tag, outside)
        env = outside / "venv"
        uv = shutil.which("uv")
        if uv is not None:
            run(
                uv,
                "--no-config",
                "venv",
                "--no-project",
                "--python",
                sys.executable,
                env,
                directory=outside,
            )
            bin_dir = env / ("Scripts" if os.name == "nt" else "bin")
            python = bin_dir / ("python.exe" if os.name == "nt" else "python")
            exe = bin_dir / ("yaga.exe" if os.name == "nt" else "yaga")
            run(
                uv,
                "--no-config",
                "pip",
                "install",
                "--python",
                python,
                "--no-deps",
                wheel,
                directory=outside,
            )
        else:
            run(sys.executable, "-I", "-m", "venv", env, directory=outside)
            bin_dir = env / ("Scripts" if os.name == "nt" else "bin")
            python = bin_dir / ("python.exe" if os.name == "nt" else "python")
            exe = bin_dir / ("yaga.exe" if os.name == "nt" else "yaga")
            run(
                python,
                "-I",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                wheel,
                directory=outside,
            )
        check = (
            "import os, pathlib, sys, yaga; "
            "exe = yaga.find_yaga_bin().resolve(); "
            "assert exe.is_relative_to(pathlib.Path(sys.prefix).resolve()); "
            "assert exe.is_file() and os.access(exe, os.X_OK)"
        )
        run(python, "-I", "-c", check, directory=outside)
        for entry in ([exe], [python, "-I", "-m", "yaga"]):
            result = subprocess.check_output([*entry, "--version"], cwd=outside, timeout=30)
            if result.splitlines() != [f"yaga {version}".encode()]:
                raise ValueError("installed native CLI version mismatch")
        policy = outside / "branch-policy.toml"
        policy.write_text('branch-policy-version = 1\nallowed-patterns = ["feat/**"]\n', "utf-8")
        run(
            exe,
            "branch",
            "check",
            "--policy",
            policy,
            "--name",
            "feat/packaging",
            directory=outside,
        )


def build(
    directory: Path,
    target: str,
    source: Path,
    *,
    prebuilt: Path | None = None,
) -> None:
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    if prebuilt is None and arch != target.split("-")[0]:
        raise ValueError("wheel build requires a matching CPU")
    tag, version = PLATFORMS[target], package_version(source)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("wheel output directory must be empty")
    with tempfile.TemporaryDirectory(prefix="yaga-wheel-build-") as temp:
        raw = Path(temp)
        env = os.environ.copy()
        if prebuilt is not None:
            env["YAGA_PREBUILT_BINARY"] = str(prebuilt.resolve(strict=True))
            env["YAGA_WHEEL_PLATFORM"] = tag
        elif tag.startswith("macosx"):
            env["_PYTHON_HOST_PLATFORM"] = f"macosx-11.0-{tag.removeprefix('macosx_11_0_')}"
            env["MACOSX_DEPLOYMENT_TARGET"] = "11.0"
        elif tag.startswith("manylinux"):
            env["YAGA_CARGO_TARGET"] = target
            env["YAGA_WHEEL_PLATFORM"] = tag
        uv = shutil.which("uv")
        if uv is not None:
            run(
                uv,
                "--no-config",
                "build",
                "--wheel",
                "--no-build-isolation",
                "--out-dir",
                raw,
                source,
                directory=raw,
                env=env,
            )
        else:
            run(
                sys.executable,
                "-I",
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                raw,
                source,
                directory=raw,
                env=env,
            )
        (wheel,) = raw.glob("*.whl")
        target_wheel = directory / wheel_name(version, tag)
        shutil.copy2(wheel, target_wheel)
    smoke(target_wheel, target, version)


def assemble(source_dist: Path, native: Path, output: Path, version: str) -> None:
    source_names = {f"yaga_cli-{version}.tar.gz"}
    native_names = {wheel_name(version, tag) for tag in PLATFORMS.values()}
    for directory, expected in ((source_dist, source_names), (native, native_names)):
        if {path.name for path in directory.iterdir()} != expected:
            raise ValueError(f"incomplete or unexpected distribution set in {directory}")
        if any(not path.is_file() or path.is_symlink() for path in directory.iterdir()):
            raise ValueError("release assets must be regular files")
    with tempfile.TemporaryDirectory(prefix="yaga-release-wheel-") as scratch:
        for tag in PLATFORMS.values():
            wheel_payload(native / wheel_name(version, tag), tag, Path(scratch))
    output.mkdir()
    for directory in (source_dist, native):
        for path in sorted(directory.iterdir()):
            shutil.copy2(path, output / path.name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("target", choices=PLATFORMS)
    builder.add_argument("directory", type=Path)
    builder.add_argument("--prebuilt", type=Path, help="Wrap an already verified standalone binary")
    verifier = commands.add_parser("smoke")
    verifier.add_argument("target", choices=PLATFORMS)
    verifier.add_argument("wheel", type=Path)
    collector = commands.add_parser("assemble")
    for name in ("source_dist", "native", "output"):
        collector.add_argument(name, type=Path)
    args = parser.parse_args()
    source = Path.cwd()
    if args.command == "build":
        build(args.directory.resolve(), args.target, source, prebuilt=args.prebuilt)
    elif args.command == "smoke":
        smoke(args.wheel.resolve(), args.target, package_version(source))
    else:
        assemble(args.source_dist, args.native, args.output, package_version(source))


if __name__ == "__main__":
    main()
