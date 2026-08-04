import re
import tomllib
from collections import Counter
from pathlib import Path

WEB_ROOT = Path(__file__).parents[1] / "src" / "mua_bot" / "web"
PROJECT_ROOT = WEB_ROOT.parents[2]


def test_javascript_element_references_exist_in_html() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    javascript = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    html_ids = re.findall(r'id="([^"]+)"', html)
    javascript_ids = set(re.findall(r'\$\("([^"]+)"\)', javascript))

    assert not [identifier for identifier, count in Counter(html_ids).items() if count > 1]
    assert javascript_ids <= set(html_ids)


def test_orchestration_workbench_contract_is_present() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    for identifier in (
        "orchestration-canvas",
        "orchestration-node-layer",
        "orchestration-edge-layer",
        "orchestration-inspector",
        "orchestration-menu",
        "orchestration-search",
        "orchestration-save",
        "record-bot",
        "record-group",
        "record-query",
        "records-prev",
        "records-next",
        "command-dialog",
        "command-query",
        "command-results",
    ):
        assert f'id="{identifier}"' in html


def test_stylesheet_braces_are_balanced() -> None:
    stylesheet = (WEB_ROOT / "app.css").read_text(encoding="utf-8")
    depth = 0
    for character in stylesheet:
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
        assert depth >= 0
    assert depth == 0


def test_web_asset_cache_version_matches_project_version() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]

    assert f"/gui/assets/app.css?v={version}" in html
    assert f"/gui/assets/app.js?v={version}" in html
