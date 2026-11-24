"""Stage a host-tested standalone YAGA executable or assemble the exact release checksum set."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess
import tomllib
from pathlib import Path

if __package__:
    from .release_images import check_pe_image, elf_program_headers, macos_deployment
else:
    from release_images import check_pe_image, elf_program_headers, macos_deployment

ROOT = Path(__file__).resolve().parents[1]
TARGETS = tuple(
    f"{arch}-{platform}"
    for arch in ("aarch64", "x86_64")
    for platform in ("apple-darwin", "pc-windows-msvc", "unknown-linux-musl")
)
MANIFEST = "yaga-native-sha256.txt"
# Each standalone executable is also committed as a file in the native Action distribution.
MAX_EXECUTABLE_BYTES = 100 * 1024**2


def asset_name(target: str) -> str:
    if target not in TARGETS:
        raise ValueError("unsupported release target")
    return f"yaga-{target}" + (".exe" if target.endswith("msvc") else "")


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    with (ROOT / "Cargo.toml").open("rb") as stream:
        cargo = tomllib.load(stream)["workspace"]["package"]["version"]
    if version != cargo or not re.fullmatch(r"(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){2}", version):
        raise ValueError("the Cargo and Python release versions must agree on final X.Y.Z")
    return version


def verify_image(path: Path, target: str) -> None:
    """Reject missing, oversized, empty, linked, mislabelled or dynamic-Linux artifacts."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise ValueError(f"invalid executable file for {target}")
    if info.st_size > MAX_EXECUTABLE_BYTES:
        raise ValueError(f"executable for {target} exceeds the 100 MiB Action distribution limit")
    arm = target.startswith("aarch64")
    try:
        if target.endswith("msvc"):
            check_pe_image(path, 0xAA64 if arm else 0x8664, executable=True)
        elif target.endswith("darwin"):
            macos_deployment(path, "arm64" if arm else "x86_64", executable=True)
        else:
            check_musl_executable(path, 183 if arm else 62)
    except ValueError as error:
        raise ValueError(
            f"executable architecture or format does not match {target}: {error}"
        ) from error


def check_musl_executable(path: Path, machine: int) -> None:
    """Accept static executables, including musl PIE with a dynamic relocation table."""
    with path.open("rb") as stream:
        kind, entry, headers = elf_program_headers(stream, path, machine)
        size = os.fstat(stream.fileno()).st_size
        # A static PIE and a shared object can both be ET_DYN. A CLI needs a
        # nonzero entry point backed by file bytes in an executable PT_LOAD.
        if (
            kind not in (2, 3)
            or not entry
            or not any(
                segment == 1
                and flags & 1
                and start + length <= size
                and length <= memory
                and address <= entry < address + length
                for segment, flags, start, address, _, length, memory, _ in headers
            )
        ):
            raise ValueError("ELF image lacks an executable entry point")
        for segment, _, start, _, _, length, _, _ in headers:
            if segment == 3:  # PT_INTERP
                raise ValueError("Linux executable requires a program interpreter")
            if segment == 2:  # PT_DYNAMIC
                if length > 65536 or length % 16 or start + length > size:
                    raise ValueError("invalid ELF dynamic section")
                stream.seek(start)
                if any(tag == 1 for tag, _ in struct.iter_unpack("<qQ", stream.read(length))):
                    raise ValueError("Linux executable requires shared libraries")


def stage(binary: Path, target: str, destination: Path) -> None:
    verify_image(binary, target)
    version = project_version()
    result = subprocess.run(
        [str(binary.resolve()), "--version"], check=True, capture_output=True, timeout=30
    )
    if result.stdout.splitlines() != [f"yaga {version}".encode("ascii")] or result.stderr:
        raise ValueError("release binary did not report the expected version")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(binary, destination / asset_name(target))


def manifest(directory: Path) -> None:
    expected = {asset_name(target) for target in TARGETS}
    if {item.name for item in directory.iterdir()} != expected:
        raise ValueError(
            "native release requires exactly one executable for each of the six targets"
        )
    records = [f"yaga-native-v1 {project_version()}"]
    for target in TARGETS:
        name = asset_name(target)
        path = directory / name
        verify_image(path, target)
        with path.open("rb") as stream:
            records.append(f"{hashlib.file_digest(stream, 'sha256').hexdigest()}  {name}")
    (directory / MANIFEST).write_bytes(("\n".join(records) + "\n").encode("ascii"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    build = commands.add_parser("stage", help="verify a runnable binary and stage it for upload")
    build.add_argument("target", choices=TARGETS)
    build.add_argument("binary", type=Path)
    build.add_argument("directory", type=Path)
    pack = commands.add_parser("manifest", help="verify and checksum the complete release")
    pack.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.operation == "stage":
        stage(args.binary, args.target, args.directory)
    else:
        manifest(args.directory)


if __name__ == "__main__":
    main()
