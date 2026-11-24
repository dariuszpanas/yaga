"""Windows and Linux release image checks can run on any controller."""

import stat
import struct
import zipfile
from pathlib import Path

import pytest

from scripts import release_wheels

OTHER_CPU_TAG = {
    "win_amd64": "win_arm64",
    "win_arm64": "win_amd64",
    "manylinux_2_28_x86_64": "manylinux_2_28_aarch64",
    "manylinux_2_28_aarch64": "manylinux_2_28_x86_64",
}


def pe_image(tag: str, *, executable: bool) -> bytes:
    cpu = 0xAA64 if tag == "win_arm64" else 0x8664
    dos = bytearray(64)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 60, len(dos))
    flags = 0x0002 if executable else 0x2002
    coff = struct.pack("<4sHHIIIHH", b"PE\0\0", cpu, 1, 0, 0, 0, 240, flags)
    return bytes(dos) + coff + struct.pack("<H", 0x020B) + bytes(238 + 40)


def elf_image(
    tag: str, *, executable: bool, fixed_address: bool = False, static_musl: bool = False
) -> bytes:
    cpu = 183 if tag.endswith("_aarch64") else 62
    if static_musl:
        types = [1]
    elif executable:
        types = [1, 3]
    else:
        # Shared library with PT_DYNAMIC (2) so it is distinguishable from a static musl binary.
        types = [1, 2]
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        b"\x7fELF\x02\x01\x01",
        2 if fixed_address else 3,
        cpu,
        1,
        0,
        64,
        0,
        0,
        64,
        56,
        len(types),
        0,
        0,
        0,
    )
    return header + b"".join(struct.pack("<I52x", kind) for kind in types)


def native_wheel(directory: Path, tag: str, *, wrong: str = "", other_cpu: bool = False) -> Path:
    windows = tag.startswith("win_")
    filename = "_bin/yaga.exe" if windows else "_bin/yaga"
    image = pe_image if windows else elf_image
    wheel = directory / release_wheels.wheel_name("1.2.3", tag)
    with zipfile.ZipFile(wheel, "w") as archive:
        info = zipfile.ZipInfo(f"yaga/{filename}")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        cpu = OTHER_CPU_TAG[tag] if wrong == "cli" and other_cpu else tag
        executable = not (wrong == "cli" and not other_cpu)
        archive.writestr(info, image(cpu, executable=executable))
        archive.writestr(
            "yaga_cli-1.2.3.dist-info/WHEEL",
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-{tag}\n",
        )
    return wheel


@pytest.mark.parametrize("tag", OTHER_CPU_TAG)
@pytest.mark.parametrize("other_cpu", [True, False], ids=["architecture", "image-role"])
def test_rejects_a_mislabelled_native_member_before_install(
    tmp_path: Path, tag: str, other_cpu: bool
) -> None:
    target = next(
        target for target, platform in release_wheels.PLATFORMS.items() if platform == tag
    )
    wheel = native_wheel(tmp_path, tag, wrong="cli", other_cpu=other_cpu)
    with pytest.raises(ValueError, match="wrong (ELF|PE)"):
        release_wheels.smoke(wheel, target, "1.2.3")


@pytest.mark.parametrize(
    ("tag", "offset", "replacement"),
    [
        ("win_arm64", 0, b"NE"),
        ("win_arm64", 60, b"\xff" * 4),
        ("win_arm64", 64, b"NE"),
        ("win_arm64", 88, b"\x0b\x01"),
        ("win_arm64", 70, b"\0\0"),
        ("win_arm64", 84, b"\xff\xff"),
        ("win_arm64", 86, b"\0\0"),
        ("manylinux_2_28_aarch64", 4, b"\x01"),
        ("manylinux_2_28_aarch64", 5, b"\x02"),
        ("manylinux_2_28_aarch64", 7, b"\x09"),
        ("manylinux_2_28_aarch64", 32, b"\xff" * 8),
        ("manylinux_2_28_aarch64", 56, b"\xff\xff"),
        ("manylinux_2_28_aarch64", 64, b"\x03"),
    ],
)
def test_rejects_malformed_native_headers(
    tmp_path: Path, tag: str, offset: int, replacement: bytes
) -> None:
    windows = tag.startswith("win_")
    image, check = (
        (pe_image, release_wheels.check_pe) if windows else (elf_image, release_wheels.check_elf)
    )
    data = bytearray(image(tag, executable=True))
    data[offset : offset + len(replacement)] = replacement
    path = tmp_path / "image"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        check(path, tag, executable=True)


def test_accepts_static_musl_and_fixed_address_glibc_executables(tmp_path: Path) -> None:
    tag = "manylinux_2_28_x86_64"
    for name, kwargs in (
        ("yaga-glibc", {"executable": True, "fixed_address": True}),
        ("yaga-musl", {"executable": True, "static_musl": True}),
    ):
        path = tmp_path / name
        path.write_bytes(elf_image(tag, **kwargs))
        release_wheels.check_elf(path, tag, executable=True)


@pytest.mark.parametrize(
    ("tag", "arm"),
    [("manylinux_2_28_x86_64", False), ("manylinux_2_28_aarch64", True)],
)
def test_accepts_static_pie_musl_and_rejects_shared_library_dynamic_table(
    tmp_path: Path, tag: str, arm: bool
) -> None:
    from tests.test_standalone_images import static_elf

    path = tmp_path / "yaga"
    pie = bytearray(static_elf(arm, pie=True))
    path.write_bytes(pie)
    release_wheels.check_elf(path, tag, executable=True)

    # Injecting DT_NEEDED (tag=1) turns the PT_DYNAMIC table into a shared-library dependency.
    with_needed = bytearray(pie)
    with_needed[176:184] = struct.pack("<q", 1)
    path.write_bytes(with_needed)
    with pytest.raises(ValueError, match="wrong ELF executable or shared-object role"):
        release_wheels.check_elf(path, tag, executable=True)


@pytest.mark.parametrize(
    ("name", "size"),
    [("other-1.2.3.dist-info/WHEEL", 60), ("yaga_cli-1.2.3.dist-info/WHEEL", 64 * 1024 + 1)],
)
def test_metadata_must_belong_to_the_wheel_and_fit_its_limit(
    tmp_path: Path, name: str, size: int
) -> None:
    tag = "win_arm64"
    wheel = native_wheel(tmp_path, tag)
    with zipfile.ZipFile(wheel) as archive:
        payloads = [
            (info, archive.read(info))
            for info in archive.infolist()
            if info.filename.startswith("yaga/")
        ]
    with zipfile.ZipFile(wheel, "w") as archive:
        for info, data in payloads:
            archive.writestr(info, data)
        archive.writestr(name, b"Root-Is-Purelib: false\nTag: py3-none-win_arm64\n".ljust(size))
    with pytest.raises(ValueError, match="invalid native wheel metadata member"):
        release_wheels.wheel_payload(wheel, tag, tmp_path)
