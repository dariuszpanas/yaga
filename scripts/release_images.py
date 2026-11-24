"""Inspect the native image headers used by the release collectors on any build host."""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import BinaryIO


def check_pe_image(path: Path, machine: int, *, executable: bool) -> None:
    """Check the MSVC PE32+ machine and DLL flag without running either payload."""
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        dos = stream.read(64)
        if len(dos) != 64 or not dos.startswith(b"MZ"):
            raise ValueError(f"{path.name} lacks a DOS image header")
        (offset,) = struct.unpack_from("<I", dos, 60)
        if not 64 <= offset <= size - 24:
            raise ValueError(f"{path.name} has an invalid PE header offset")
        stream.seek(offset)
        signature, cpu, sections, _, _, _, optional, flags = struct.unpack(
            "<4sHHIIIHH", stream.read(24)
        )
        if signature != b"PE\0\0" or cpu != machine:
            raise ValueError(f"{path.name} has the wrong PE signature or machine")
        if (
            not sections
            or optional < 112
            or offset + 24 + optional + 40 * sections > size
            or stream.read(2) != b"\x0b\x02"
        ):
            raise ValueError(f"{path.name} has an invalid PE32+ image header")
        if not flags & 0x0002 or bool(flags & 0x2000) == executable:
            raise ValueError(f"{path.name} has the wrong PE executable or DLL characteristics")


def elf_program_headers(
    stream: BinaryIO, path: Path, machine: int
) -> tuple[int, int, list[tuple[int, ...]]]:
    """Read the ELF64 image type, entry point and bounded program header table."""
    head = stream.read(64)
    if len(head) != 64 or not head.startswith(b"\x7fELF\x02\x01\x01") or head[7] not in (0, 3):
        raise ValueError(f"{path.name} requires a 64-bit little-endian Linux ELF header")
    _, kind, cpu, version, entry, offset, _, _, header_size, entry_size, count, _, _, _ = (
        struct.unpack("<16sHHIQQQIHHHHHH", head)
    )
    if cpu != machine or version != 1:
        raise ValueError(f"{path.name} has the wrong ELF machine or version")
    size = os.fstat(stream.fileno()).st_size
    if (
        header_size != 64
        or entry_size != 56
        or not 0 < count <= 512
        or not 64 <= offset <= size - count * entry_size
    ):
        raise ValueError(f"{path.name} has invalid ELF program headers")
    stream.seek(offset)
    return kind, entry, list(struct.iter_unpack("<IIQQQQQQ", stream.read(count * entry_size)))


def check_musl_executable_headers(
    stream: BinaryIO,
    kind: int,
    entry: int,
    headers: list[tuple[int, ...]],
) -> None:
    """Validate static and static-PIE musl ELF headers without shared-library dependencies."""
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


def check_musl_executable(path: Path, machine: int) -> None:
    """Accept static executables, including musl PIE with a dynamic relocation table."""
    with path.open("rb") as stream:
        kind, entry, headers = elf_program_headers(stream, path, machine)
        check_musl_executable_headers(stream, kind, entry, headers)


def macos_deployment(path: Path, arch: str, *, executable: bool) -> tuple[int, int, int]:
    """Validate a thin macOS image of the requested role and read its minimum OS."""
    cpu, subtype = {"x86_64": (0x01000007, 3), "arm64": (0x0100000C, 0)}[arch]
    with path.open("rb") as stream:
        header = stream.read(32)
        if len(header) != 32 or header[:4] != b"\xcf\xfa\xed\xfe":
            raise ValueError(f"{path.name} must be a thin little-endian 64-bit Mach-O for {arch}")
        _, actual_cpu, actual_subtype, kind, count, size, _, _ = struct.unpack("<8I", header)
        # The high LIB64 capability bit is also used in generic x86-64 images.
        if arch == "x86_64":
            actual_subtype &= ~0x80000000
        if (actual_cpu, actual_subtype) != (cpu, subtype):
            raise ValueError(f"{path.name} has the wrong Mach-O CPU or subtype for {arch}")
        if kind not in ((2,) if executable else (6, 8)):  # MH_EXECUTE or MH_DYLIB / MH_BUNDLE
            raise ValueError(f"{path.name} has the wrong Mach-O file type")
        if size > os.fstat(stream.fileno()).st_size - 32 or count > size // 8:
            raise ValueError(f"{path.name} has an invalid Mach-O load command table")
        commands = stream.read(size)
        if len(commands) != size:
            raise ValueError(f"{path.name} has a truncated Mach-O load command table")

    offset = 0
    minimum = None
    for _ in range(count):
        if offset + 8 > len(commands):
            raise ValueError(f"{path.name} has a truncated Mach-O load command")
        command, length = struct.unpack_from("<II", commands, offset)
        if length < 8 or length % 8 or length > len(commands) - offset:
            raise ValueError(f"{path.name} has an invalid Mach-O load command size")
        if command in (0x25, 0x2F, 0x30):  # Legacy iOS, tvOS and watchOS minimum commands
            raise ValueError(f"{path.name} declares a non-macOS platform")
        if command in (0x24, 0x32):  # LC_VERSION_MIN_MACOSX / LC_BUILD_VERSION
            if minimum is not None:
                raise ValueError(f"{path.name} declares multiple Mach-O deployment targets")
            if command == 0x24:
                if length != 16:
                    raise ValueError(f"{path.name} has an invalid LC_VERSION_MIN_MACOSX")
                (version,) = struct.unpack_from("<I", commands, offset + 8)
            else:
                if length < 24:
                    raise ValueError(f"{path.name} has a truncated LC_BUILD_VERSION")
                platform_id, version, _, tools = struct.unpack_from("<4I", commands, offset + 8)
                if length != 24 + 8 * tools:
                    raise ValueError(f"{path.name} has an invalid LC_BUILD_VERSION tool count")
                if platform_id != 1:  # PLATFORM_MACOS
                    raise ValueError(f"{path.name} declares a non-macOS platform")
            minimum = (version >> 16, version >> 8 & 0xFF, version & 0xFF)
        offset += length
    if offset != len(commands) or minimum is None:
        raise ValueError(
            f"{path.name} lacks a complete Mach-O table with one macOS deployment target"
        )
    return minimum
