"""Verify the image roles promised by the six standalone installer assets."""

import struct
from pathlib import Path

import pytest

from scripts.native_release import MANIFEST, TARGETS, asset_name, manifest, verify_image
from tests.test_macos_release_wheels import ARM, INTEL, image, legacy
from tests.test_release_wheel_images import pe_image


def static_elf(arm: bool, *, pie: bool) -> bytes:
    address = 0x10000
    count = 2 if pie else 1
    size = 64 + count * 56
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        b"\x7fELF\x02\x01\x01",
        3 if pie else 2,
        183 if arm else 62,
        1,
        address + size,
        64,
        0,
        0,
        64,
        56,
        count,
        0,
        0,
        0,
    )
    load = struct.pack("<IIQQQQQQ", 1, 5, 0, address, 0, size + 16, size + 16, 4096)
    # A static PIE can carry PT_DYNAMIC with no DT_NEEDED entries.
    dynamic = struct.pack("<IIQQQQQQ", 2, 4, size, address + size, 0, 16, 16, 8) if pie else b""
    return header + load + dynamic + bytes(16)


@pytest.mark.parametrize("swapped", [None, *TARGETS])
def test_manifest_requires_standalone_image_roles(tmp_path: Path, swapped: str | None) -> None:
    for target in TARGETS:
        arm = target.startswith("aarch64")
        standalone = target != swapped
        if target.endswith("msvc"):
            data = pe_image("win_arm64" if arm else "win_amd64", executable=standalone)
        elif target.endswith("darwin"):
            # A standalone binary has no wheel tag ceiling.
            data = image(
                legacy(12 << 16),
                cpu=ARM if arm else INTEL,
                subtype=0 if arm else 3,
                kind=2 if standalone else 6,
            )
        else:
            data = bytearray(static_elf(arm, pie=arm))
            if not standalone:
                struct.pack_into("<Q", data, 24, 0)  # shared object without an entry point
        (tmp_path / asset_name(target)).write_bytes(data)
    if swapped is None:
        manifest(tmp_path)
        assert len((tmp_path / MANIFEST).read_text().splitlines()) == 7
    else:
        with pytest.raises(ValueError, match=swapped):
            manifest(tmp_path)
        assert not (tmp_path / MANIFEST).exists()


@pytest.mark.parametrize(
    ("offset", "replacement", "message"),
    [
        (16, struct.pack("<H", 1), "entry point"),  # ET_REL
        (24, struct.pack("<Q", 0x20000), "entry point"),  # outside PT_LOAD
        (64, struct.pack("<I", 4), "entry point"),  # PT_NOTE only
        (68, struct.pack("<I", 4), "entry point"),  # loadable without execute permission
        (120, struct.pack("<I", 3), "interpreter"),
        (176, struct.pack("<q", 1), "shared libraries"),  # DT_NEEDED
    ],
)
def test_musl_executable_validation(
    tmp_path: Path, offset: int, replacement: bytes, message: str
) -> None:
    data = bytearray(static_elf(True, pie=True))
    data[offset : offset + len(replacement)] = replacement
    path = tmp_path / "yaga"
    path.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        verify_image(path, "aarch64-unknown-linux-musl")
