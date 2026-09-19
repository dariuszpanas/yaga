"""Check built documentation and README links against the same offline site tree."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

if __package__:
    from .check_description import DescriptionLinks, validate_description
else:
    from check_description import DescriptionLinks, validate_description

SITE = "https://dariuszpanas.github.io/yaga/"


def check_site(site: Path, readme: Path) -> int:
    """Reject missing same-site files and fragment targets, including package links."""
    site = site.resolve()
    pages: dict[Path, DescriptionLinks] = {}
    for path in site.rglob("*.html"):
        parsed = DescriptionLinks()
        parsed.feed(path.read_text(encoding="utf-8"))
        pages[path] = parsed
    if not pages:
        raise ValueError("build the documentation before checking its links")
    description = DescriptionLinks()
    description.feed(validate_description(readme.read_text(encoding="utf-8")))
    checked = 0
    sources = [(str(path.relative_to(site)), page) for path, page in pages.items()]
    sources.append(("README", description))
    for relative, page in sources:
        source_url = urljoin(SITE, relative.replace("\\", "/"))
        for link in page.links:
            if relative == "README" and link.startswith("#"):
                continue  # validate_description already checks the description's own anchors.
            resolved = urljoin(source_url, link)
            if not resolved.startswith(SITE):
                continue
            url = urlsplit(resolved)
            local = unquote(url.path.removeprefix("/yaga/"))
            target = (site / local).resolve()
            if not target.is_relative_to(site):
                raise ValueError(f"{relative}: link escapes the site: {link}")
            if target.is_dir():
                target /= "index.html"
            if not target.is_file():
                raise ValueError(f"{relative}: missing link target: {link}")
            if url.fragment and target in pages:
                if unquote(url.fragment) not in pages[target].anchors:
                    raise ValueError(f"{relative}: missing fragment target: {link}")
            checked += 1
    return checked


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    count = check_site(root / "site", root / "README.md")
    print(f"Checked {count} local documentation and README links.")
