"""Regressions for the description embedded in release artifacts."""

import pytest

from scripts.check_description import LOGO_URL, validate_description

LOGO = f'<img src="{LOGO_URL}" alt="YAGA terminal Y logo" width="128" height="128">'


def test_rendered_description_preserves_logo_and_absolute_links() -> None:
    rendered = validate_description(LOGO + "\n\n[Guide](https://example.org/guide.html)")
    assert LOGO_URL in rendered
    assert 'width="128"' in rendered


@pytest.mark.parametrize(
    "link",
    ["docs/guide.md", "/guide", "//example.org/guide", "http://example.org/guide"],
)
def test_relative_or_insecure_links_are_rejected(link: str) -> None:
    with pytest.raises(SystemExit, match="absolute HTTPS"):
        validate_description(LOGO + f"\n\n[Guide]({link})")


def test_private_repository_logo_is_rejected() -> None:
    with pytest.raises(SystemExit, match="public documentation logo"):
        validate_description(
            LOGO.replace(LOGO_URL, "https://raw.githubusercontent.com/a/b/main/logo.png")
        )


def test_missing_logo_is_rejected() -> None:
    with pytest.raises(SystemExit, match="public documentation logo"):
        validate_description("# YAGA")


def test_missing_internal_anchor_is_rejected() -> None:
    with pytest.raises(SystemExit, match="missing anchor"):
        validate_description(LOGO + "\n\n[Missing](#does-not-exist)")


def test_heading_links_resolve_after_rendering() -> None:
    validate_description(LOGO + "\n\n## Usage\n\n[Usage](#usage)")
