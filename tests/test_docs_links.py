"""Offline rendered-site checks catch broken README routes and documentation anchors."""

from pathlib import Path

import pytest

from scripts.check_description import LOGO_URL
from scripts.check_docs import check_site


@pytest.mark.parametrize("link", ["guide.html", "guide.html#missing", "missing.png"])
def test_missing_documentation_targets_fail(tmp_path: Path, link: str) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(f'<a href="{link}">Guide</a>', encoding="utf-8")
    if "#" in link:
        (site / "guide.html").write_text('<h1 id="present">Guide</h1>', encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(f'<img src="{LOGO_URL}" alt="YAGA terminal Y logo">', encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        check_site(site, readme)


def test_readme_routes_share_the_built_site(tmp_path: Path) -> None:
    logo = tmp_path / "site/assets/branding/isometric_terminal_y_logo_transparent.png"
    logo.parent.mkdir(parents=True)
    logo.write_bytes(b"image fixture")
    (tmp_path / "site/index.html").write_text('<h1 id="start">Start</h1>', encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(
        f'<img src="{LOGO_URL}" alt="YAGA terminal Y logo">\n\n'
        "[Start](https://dariuszpanas.github.io/yaga/#start)",
        encoding="utf-8",
    )
    assert check_site(tmp_path / "site", readme) == 2
    readme.write_text(readme.read_text().replace("#start", "#absent"), encoding="utf-8")
    with pytest.raises(ValueError, match="missing fragment"):
        check_site(tmp_path / "site", readme)
