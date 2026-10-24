"""Keeps the README, docs, and root community files consistent and publishable.

Checks: the README product table matches the catalog, relative links and
anchors resolve, docs/README.md lists every page, no em dashes, no remote
hosting instructions, and no personal details (real email addresses or
home-directory paths).
"""

import re
from pathlib import Path

import pytest

from googlemcp.catalog import load_catalog

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DOCS_INDEX = ROOT / "docs" / "README.md"
MAX_README_LINES = 250
ROOT_PAGES = ("CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md")


def _pages() -> list[Path]:
    """Every published Markdown page: README, docs/*.md, and root community files."""
    pages = [README, *sorted((ROOT / "docs").glob("*.md"))]
    return pages + [ROOT / name for name in ROOT_PAGES if (ROOT / name).exists()]


def _page_id(page: Path) -> str:
    return page.relative_to(ROOT).as_posix()


# Remote-hosting instructions that must not appear: the server is local stdio
# only. Text under a "## History" heading may describe the earlier remote
# design.
REMOTE_HOSTING_TERMS = (
    "Cloud Run",
    "gcloud run",
    "docker run",
    "docker build",
    "Artifact Registry",
    "GOOGLEMCP_TRANSPORT=http",
    "--transport http",
    "GOOGLEMCP_AUTH_MODE",
    "auth_mode",
    "GOOGLEMCP_ALLOWED_EMAILS",
    "GOOGLEMCP_PUBLIC_BASE_URL",
    "mcp-bearer-token",
    "token generate",
    "FASTMCP_HOME",
    "enable_runtime",
)
# (file, term) pairs where a page names a hosting term only to say it is absent
# or forbidden. Keep this list short and explicit; each entry needs a reason.
NEGATIVE_MENTIONS = {
    # "Nothing here hosts the server: there is no Cloud Run, Artifact Registry, ..."
    # and the policy-test description of rejected resources.
    ("docs/terraform.md", "Cloud Run"),
    ("docs/terraform.md", "Artifact Registry"),
}

ROW_RE = re.compile(r"^\|\s*`(?P<key>[a-z_]+)`\s*\|(?P<rest>.*)\|\s*$")
LINK_RE = re.compile(r"\]\((?P<target>[^)\s]+)\)")


def _product_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        match = ROW_RE.match(line)
        if match:
            key = match.group("key")
            assert key not in rows, f"duplicate README row for {key}"
            rows[key] = [cell.strip() for cell in match.group("rest").split("|")]
    return rows


def test_product_table_matches_catalog() -> None:
    catalog = load_catalog()
    rows = _product_rows()
    assert set(rows) == set(catalog)
    for key, product in catalog.items():
        title, default, why_off = rows[key]
        assert title == product.title, key
        assert default == ("on" if product.default_enabled else "off"), key
        if product.default_enabled:
            assert why_off == "", f"{key} is on by default but has a reason"
        else:
            assert why_off, f"{key} is off by default and needs a reason"


def _slug(heading: str) -> str:
    """GitHub-style heading anchor."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return {_slug(line.lstrip("#")) for line in lines if line.startswith("#")}


def _links(page: Path) -> list[tuple[Path, str]]:
    found = []
    for match in LINK_RE.finditer(page.read_text(encoding="utf-8")):
        target = match.group("target")
        if re.match(r"^[a-z]+:", target):
            continue
        path_part, _, anchor = target.partition("#")
        resolved = (page.parent / path_part).resolve() if path_part else page
        found.append((resolved, anchor))
    return found


@pytest.mark.parametrize("page", _pages(), ids=_page_id)
def test_relative_links_resolve(page: Path) -> None:
    for target, anchor in _links(page):
        rel = target.relative_to(ROOT).as_posix()
        assert target.exists(), f"{_page_id(page)} links to missing {rel}"
        if anchor:
            assert anchor in _anchors(target), f"{_page_id(page)}: no heading #{anchor} in {rel}"


def test_docs_index_lists_every_doc() -> None:
    linked = {t.relative_to(ROOT).as_posix() for t, _ in _links(DOCS_INDEX)}
    on_disk = {p.relative_to(ROOT).as_posix() for p in (ROOT / "docs").glob("*.md")}
    on_disk.discard("docs/README.md")
    assert on_disk <= linked, f"docs/README.md is missing {sorted(on_disk - linked)}"


@pytest.mark.parametrize("page", _pages(), ids=_page_id)
def test_no_em_dashes(page: Path) -> None:
    em_dash = chr(0x2014)
    assert em_dash not in page.read_text(encoding="utf-8")


def test_readme_stays_short() -> None:
    assert len(README.read_text(encoding="utf-8").splitlines()) <= MAX_README_LINES


def _without_history(text: str) -> str:
    """Drop the body of any "## History" section (up to the next level-2 heading)."""
    return re.sub(r"^## History\n.*?(?=^## |\Z)", "", text, flags=re.MULTILINE | re.DOTALL)


@pytest.mark.parametrize("page", _pages(), ids=_page_id)
def test_no_remote_hosting_instructions(page: Path) -> None:
    rel = page.relative_to(ROOT).as_posix()
    text = _without_history(page.read_text(encoding="utf-8"))
    found = [t for t in REMOTE_HOSTING_TERMS if t in text and (rel, t) not in NEGATIVE_MENTIONS]
    assert not found, f"{rel} mentions remote hosting: {found}"


def test_history_note_is_the_only_exemption() -> None:
    text = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert "## History" in text
    assert _without_history("## History\nCloud Run\n## Next\nok\n") == "## Next\nok\n"


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Placeholder and bot addresses that are fine to publish.
ALLOWED_EMAIL_DOMAINS = {"example.com", "users.noreply.github.com"}
HOME_PATH_RE = re.compile(r"(?<![\w.-])(?:/Users/|/home/)(?!you\b)[A-Za-z0-9._-]+")


@pytest.mark.parametrize("page", _pages(), ids=_page_id)
def test_no_personal_details(page: Path) -> None:
    """Docs are written for any user: placeholders only, no real addresses or home paths."""
    text = page.read_text(encoding="utf-8")
    emails = [
        e for e in EMAIL_RE.findall(text) if e.split("@", 1)[1].lower() not in ALLOWED_EMAIL_DOMAINS
    ]
    assert not emails, f"{_page_id(page)} contains email addresses: {emails}"
    paths = HOME_PATH_RE.findall(text)
    assert not paths, f"{_page_id(page)} contains home-directory paths: {paths}"


def test_personal_details_check_catches_examples() -> None:
    assert EMAIL_RE.findall("write to someone@gmail.com") == ["someone@gmail.com"]
    assert not EMAIL_RE.findall("a personal @gmail.com account")
    assert HOME_PATH_RE.findall("see /Users/alice/repo") == ["/Users/alice"]
    assert not HOME_PATH_RE.findall("/Users/you/repo")
    assert not HOME_PATH_RE.findall("https://script.google.com/home/usersettings")
