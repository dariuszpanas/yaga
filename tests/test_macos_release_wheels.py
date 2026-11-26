"""Check macOS wheel architecture and deployment promises on any controller."""

import stat
import struct
import zipfile
from pathlib import Path

import pytest

from scripts import release_wheels
from tests.test_release_wheel_images import native_wheel

ARM, INTEL = 0x0100000C, 0x01000007
MACOS_11 = 11 << 16


def build_version(version: int, *, platform: int = 1, tools: int = 1, length: int = 32) -> bytes:
    # A newer build SDK is allowed. Only minos declares the deployment target.
    return struct.pack("<8I", 0x32, length, platform, version, 15 << 16, tools, 3, 0)


def legacy(version: int, command: int = 0x24) -> bytes:
    return struct.pack("<4I", command, 16, version, 15 << 16)


def image(
    *commands: bytes,
    cpu: int = ARM,
    subtype: int = 0,
    kind: int = 6,
    count: int | None = None,
    size: int | None = None,
) -> bytes:
    table = b"".join(commands)
    return (
        struct.pack(
            "<8I",
            0xFEEDFACF,
            cpu,
            subtype,
            kind,
            len(commands) if count is None else count,
            len(table) if size is None else size,
            0,
            0,
        )
        + table
    )


@pytest.mark.parametrize(
    ("arch", "data"),
    [
        ("arm64", image(build_version(MACOS_11), kind=2)),
        ("x86_64", image(legacy(0x0A0900), cpu=INTEL, subtype=3, kind=2)),
        (
            "x86_64",
            image(
                struct.pack("<II", 0x1B, 24) + bytes(16),
                build_version(MACOS_11),
                cpu=INTEL,
                subtype=0x80000003,
                kind=2,
            ),
        ),
    ],
)
def test_accepts_supported_thin_macos_executable(tmp_path: Path, arch: str, data: bytes) -> None:
    path = tmp_path / "image"
    path.write_bytes(data)
    release_wheels.check_macos(path, f"macosx_11_0_{arch}")


@pytest.mark.parametrize(
    "data",
    [
        b"",
        bytes.fromhex("cafebabe") + bytes(28),
        bytes.fromhex("cafebabf") + bytes(28),
        image(build_version(MACOS_11), cpu=INTEL, subtype=3, kind=2),
        image(build_version(MACOS_11), subtype=2, kind=2),  # arm64e
        image(build_version(MACOS_11), kind=1),  # MH_OBJECT
        image(build_version(MACOS_11), kind=6),  # MH_DYLIB shared library
        image(build_version(MACOS_11), kind=8),  # MH_BUNDLE loadable bundle
        image(build_version(MACOS_11 + 1), kind=2),  # Exceeds macOS 11.0.0 tag.
        image(legacy(0x0A0F00), kind=2),  # arm64 requires 11.0 or later.
        image(build_version(0), kind=2),
        image(kind=2),
        image(struct.pack("<II", 0x1B, 24) + bytes(16), kind=2),
        image(build_version(MACOS_11), build_version(MACOS_11), kind=2),
        image(legacy(MACOS_11), build_version(MACOS_11), kind=2),
        image(build_version(MACOS_11, platform=6), kind=2),  # Mac Catalyst
        image(build_version(MACOS_11, platform=7), kind=2),  # iOS simulator
        image(legacy(MACOS_11), legacy(MACOS_11, 0x25), kind=2),
        image(legacy(MACOS_11, 0x2F), kind=2),
        image(legacy(MACOS_11, 0x30), kind=2),
        image(build_version(MACOS_11), size=40, kind=2),
        image(legacy(MACOS_11), size=8, kind=2),
        image(legacy(MACOS_11), count=0, kind=2),
        image(legacy(MACOS_11), count=2, kind=2),
        image(legacy(MACOS_11), count=3, kind=2),
        image(struct.pack("<II", 0x24, 8), kind=2),
        image(struct.pack("<II", 0x32, 16) + bytes(8), kind=2),
        image(build_version(MACOS_11, tools=2), kind=2),
        image(struct.pack("<II", 0x1B, 0), kind=2),
        image(struct.pack("<II", 0x1B, 9) + bytes(1), kind=2),
    ],
)
def test_refuses_unverified_macos_member(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "member"
    path.write_bytes(data)
    with pytest.raises(ValueError, match="member"):
        release_wheels.check_macos(path, "macosx_11_0_arm64")


def test_refuses_haswell_only_executable_in_generic_intel_wheel(tmp_path: Path) -> None:
    path = tmp_path / "yaga"
    path.write_bytes(image(legacy(0x0A0F00), cpu=INTEL, subtype=8, kind=2))
    with pytest.raises(ValueError, match="wrong Mach-O CPU or subtype"):
        release_wheels.check_macos(path, "macosx_11_0_x86_64")


def macos_wheel(directory: Path, arch: str, bad: str = "", extra_tag: str = "") -> Path:
    tag = f"macosx_11_0_{arch}"
    cpu, subtype = (ARM, 0) if arch == "arm64" else (INTEL, 3)
    wheel = directory / release_wheels.wheel_name("1.2.3", tag)
    with zipfile.ZipFile(wheel, "w") as archive:
        info = zipfile.ZipInfo("yaga/_bin/yaga")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        version = 12 << 16 if bad == "cli" else MACOS_11
        archive.writestr(
            info,
            image(build_version(version), cpu=cpu, subtype=subtype, kind=2),
        )
        archive.writestr(
            "yaga_cli-1.2.3.dist-info/WHEEL",
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-{tag}\n{extra_tag}",
        )
        archive.writestr(
            "yaga_cli-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: yaga-cli\nVersion: 1.2.3\n",
        )
    return wheel


@pytest.mark.parametrize(
    ("arch", "bad", "extra_tag", "message"),
    [
        ("x86_64", "cli", "", "yaga declares macOS 12"),
        ("arm64", "", "Tag: py3-none-macosx_11_0_universal2\n", "metadata"),
    ],
)
def test_release_refuses_wheel_before_install(
    tmp_path: Path, arch: str, bad: str, extra_tag: str, message: str
) -> None:
    target = "aarch64-apple-darwin" if arch == "arm64" else "x86_64-apple-darwin"
    wheel = macos_wheel(tmp_path, arch, bad, extra_tag)
    with pytest.raises(ValueError, match=message):
        release_wheels.smoke(wheel, target, "1.2.3")


def test_collect_verifies_native_members_before_copying_the_release(tmp_path: Path) -> None:
    sdist, native, output = tmp_path / "sdist", tmp_path / "native", tmp_path / "out"
    sdist.mkdir()
    native.mkdir()
    (sdist / "yaga_cli-1.2.3.tar.gz").write_bytes(b"source archive")
    for platform in release_wheels.PLATFORMS.values():
        if not platform.startswith("macosx"):
            native_wheel(native, platform)
    macos_wheel(native, "arm64")
    with pytest.raises(ValueError, match="distribution set"):
        release_wheels.assemble(sdist, native, output, "1.2.3")
    macos_wheel(native, "x86_64", bad="cli")
    with pytest.raises(ValueError, match="yaga declares macOS 12"):
        release_wheels.assemble(sdist, native, output, "1.2.3")
    assert not output.exists()
    macos_wheel(native, "x86_64")
    for tag in ("win_arm64", release_wheels.PLATFORMS["x86_64-unknown-linux-musl"]):
        wheel = native_wheel(native, tag, wrong="cli", other_cpu=True)
        with pytest.raises(ValueError, match="wrong (ELF|PE)"):
            release_wheels.assemble(sdist, native, output, "1.2.3")
        wheel.write_bytes(b"not a zip file")
        with pytest.raises(zipfile.BadZipFile):
            release_wheels.assemble(sdist, native, output, "1.2.3")
        assert not output.exists()
        native_wheel(native, tag)
    obsolete = sdist / "yaga_cli-1.2.3-py3-none-any.whl"
    obsolete.touch()
    with pytest.raises(ValueError, match="distribution set"):
        release_wheels.assemble(sdist, native, output, "1.2.3")
    assert not output.exists()
    obsolete.unlink()
    release_wheels.assemble(sdist, native, output, "1.2.3")
    inputs = {
        path.name: path.read_bytes() for folder in (sdist, native) for path in folder.iterdir()
    }
    assert len(inputs) == 7
    assert {path.name: path.read_bytes() for path in output.iterdir()} == inputs
