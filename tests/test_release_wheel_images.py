"""Windows and Linux release image checks can run on any controller."""

import stat
import struct
import zipfile
from pathlib import Path

import pytest

from scripts import release_wheels

LINUX_X64 = release_wheels.PLATFORMS["x86_64-unknown-linux-musl"]
LINUX_ARM64 = release_wheels.PLATFORMS["aarch64-unknown-linux-musl"]

OTHER_CPU_TAG = {
    "win_amd64": "win_arm64",
    "win_arm64": "win_amd64",
    LINUX_X64: LINUX_ARM64,
    LINUX_ARM64: LINUX_X64,
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
    tag: str, *, executable: bool, pie: bool = False, dynamic_interp: bool = False
) -> bytes:
    from tests.test_standalone_images import static_elf

    arm = tag.endswith("_aarch64")
    if dynamic_interp:
        cpu = 183 if arm else 62
        types = [1, 3]
        header = struct.pack(
            "<16sHHIQQQIHHHHHH",
            b"\x7fELF\x02\x01\x01",
            3,
            cpu,
            1,
            0x10000,
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
    data = bytearray(static_elf(arm, pie=pie))
    if not executable:
        struct.pack_into("<Q", data, 24, 0)  # shared object without an entry point
    return bytes(data)


def native_wheel(
    directory: Path,
    tag: str,
    *,
    wrong: str = "",
    other_cpu: bool = False,
    requires_dist: str = "",
) -> Path:
    windows = tag.startswith("win_")
    filename = "_bin/yaga.exe" if windows else "_bin/yaga"
    image = pe_image if windows else elf_image
    wheel = directory / release_wheels.wheel_name("1.2.3", tag)
    tags = "".join(f"Tag: {entry}\n" for entry in release_wheels.wheel_tags(tag))
    metadata = "Metadata-Version: 2.4\nName: yaga-cli\nVersion: 1.2.3\n"
    if requires_dist:
        metadata += f"Requires-Dist: {requires_dist}\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        info = zipfile.ZipInfo(f"yaga/{filename}")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        cpu = OTHER_CPU_TAG[tag] if wrong == "cli" and other_cpu else tag
        executable = not (wrong == "cli" and not other_cpu)
        archive.writestr(info, image(cpu, executable=executable))
        archive.writestr(
            "yaga_cli-1.2.3.dist-info/WHEEL",
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\n{tags}",
        )
        archive.writestr("yaga_cli-1.2.3.dist-info/METADATA", metadata)
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
        (LINUX_ARM64, 4, b"\x01"),
        (LINUX_ARM64, 5, b"\x02"),
        (LINUX_ARM64, 7, b"\x09"),
        (LINUX_ARM64, 32, b"\xff" * 8),
        (LINUX_ARM64, 56, b"\xff\xff"),
        (LINUX_ARM64, 64, b"\x03"),
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
        check(path, tag)


def test_accepts_static_musl_and_rejects_dynamic_glibc_executables(tmp_path: Path) -> None:
    path = tmp_path / "yaga-musl"
    path.write_bytes(elf_image(LINUX_X64, executable=True, pie=False))
    release_wheels.check_elf(path, LINUX_X64)

    dynamic = tmp_path / "yaga-glibc"
    dynamic.write_bytes(elf_image(LINUX_X64, executable=True, dynamic_interp=True))
    with pytest.raises(ValueError, match="wrong ELF executable or shared-object role"):
        release_wheels.check_elf(dynamic, LINUX_X64)


@pytest.mark.parametrize(
    ("tag", "arm"),
    [(LINUX_X64, False), (LINUX_ARM64, True)],
)
def test_accepts_static_pie_musl_and_rejects_shared_library_dynamic_table(
    tmp_path: Path, tag: str, arm: bool
) -> None:
    from tests.test_standalone_images import static_elf

    path = tmp_path / "yaga"
    pie = bytearray(static_elf(arm, pie=True))
    path.write_bytes(pie)
    release_wheels.check_elf(path, tag)

    # Injecting DT_NEEDED (tag=1) turns the PT_DYNAMIC table into a shared-library dependency.
    with_needed = bytearray(pie)
    with_needed[176:184] = struct.pack("<q", 1)
    path.write_bytes(with_needed)
    with pytest.raises(ValueError, match="wrong ELF executable or shared-object role"):
        release_wheels.check_elf(path, tag)


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
            if info.filename.startswith("yaga/") or info.filename.endswith("/METADATA")
        ]
    with zipfile.ZipFile(wheel, "w") as archive:
        for info, data in payloads:
            archive.writestr(info, data)
        archive.writestr(name, b"Root-Is-Purelib: false\nTag: py3-none-win_arm64\n".ljust(size))
    with pytest.raises(ValueError, match="invalid native wheel metadata member"):
        release_wheels.wheel_payload(wheel, tag, tmp_path)


def test_wheel_payload_rejects_runtime_dependencies(tmp_path: Path) -> None:
    wheel = native_wheel(tmp_path, LINUX_X64, requires_dist="pydantic>=2")
    with pytest.raises(ValueError, match="zero runtime dependencies"):
        release_wheels.wheel_payload(wheel, LINUX_X64, tmp_path)
