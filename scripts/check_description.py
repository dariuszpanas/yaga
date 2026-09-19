"""Validate the rendered package description without network access."""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urlsplit

from readme_renderer.markdown import render

LOGO_URL = (
    "https://dariuszpanas.github.io/yaga/assets/branding/isometric_terminal_y_logo_transparent.png"
)


class DescriptionLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.images: list[dict[str, str | None]] = []
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if anchor := attributes.get("id"):
            self.anchors.add(anchor)
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"] or "")
        if tag == "img":
            self.images.append(attributes)
            self.links.append(attributes.get("src") or "")


def validate_description(description: str) -> str:
    """Return PyPI-compatible HTML; reject nonportable links and missing branding."""
    html = render(description)
    if not html:
        raise SystemExit("package description did not render")
    parsed = DescriptionLinks()
    parsed.feed(html)
    for link in parsed.links:
        if link.startswith("#"):
            if link[1:] not in parsed.anchors:
                raise SystemExit(f"package description has a missing anchor: {link}")
            continue
        url = urlsplit(link)
        if url.scheme != "https" or not url.netloc:
            raise SystemExit(f"package description requires absolute HTTPS links: {link}")
    if not any(
        image.get("src") == LOGO_URL and image.get("alt") == "YAGA terminal Y logo"
        for image in parsed.images
    ):
        raise SystemExit("package description must retain the public documentation logo")
    return html
