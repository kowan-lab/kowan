"""Build the website into build/site: the static pages of site/ plus the
documentation, rendered from the Markdown pages in site/_docs/.

    pip install -r requirements-site.txt
    python3 scripts/build_site.py            # writes build/site
    python3 scripts/serve_site.py            # builds, serves and rebuilds on change

site/_docs/nav.json lists the sections and their pages in order. Each page
starts with a front matter block:

    ---
    title: Installation
    description: One sentence for search results and link previews.
    ---

Links between pages are written as ``installation.md`` or
``installation.md#ports`` and become ``/docs/installation/#ports``; a link to
a page that does not exist, or to a heading it does not have, fails the
build. The changelog page is read from docs/CHANGELOG.md. Besides the pages,
the build writes the search index (docs/search.json) and sitemap.xml.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
SOURCE = SITE / "_docs"
OUT = ROOT / "build" / "site"
CHANGELOG = ROOT / "docs" / "CHANGELOG.md"
BASE_URL = "https://kowan.io"
REPO = "https://github.com/kowan-lab/kowan"

EXTENSIONS = ["fenced_code", "tables", "admonition", "attr_list", "def_list", "sane_lists", "toc", "md_in_html"]
EXT_CONFIG = {"toc": {"toc_depth": "2-3", "permalink": False}}

LINK = re.compile(r'href="([a-z0-9-]+)\.md(#[^"]*)?"')
TAGS = re.compile(r"<[^>]+>")


class BuildError(Exception):
    pass


@dataclass
class Page:
    slug: str
    section: str
    title: str
    description: str
    source: Path
    body: str = ""
    toc: list = field(default_factory=list)
    text: str = ""

    @property
    def url(self) -> str:
        return "/docs/" if self.slug == "index" else f"/docs/{self.slug}/"

    @property
    def edit_url(self) -> str:
        return f"{REPO}/edit/main/{self.source.relative_to(ROOT).as_posix()}"


def front_matter(text: str, where: Path) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise BuildError(f"{where}: missing front matter")
    head, _, body = text[4:].partition("\n---\n")
    meta = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    for key in ("title", "description"):
        if not meta.get(key):
            raise BuildError(f"{where}: front matter needs a {key}")
    return meta, body


def changelog_source() -> str:
    """docs/CHANGELOG.md as a docs page: its releases, without the dated
    notes of the development before 0.1.0."""
    text = CHANGELOG.read_text()
    text = text.split("\n", 1)[1]  # the "# Kowan — Changelog" title
    cut = re.search(r"^## \d{4}-\d{2}-\d{2} ", text, re.M)
    if cut:
        text = text[: cut.start()]
    return (
        "---\ntitle: Changelog\n"
        "description: What changed in each Kowan release: new features first, then fixes and other changes.\n"
        "---\n" + text
    )


def load_pages() -> tuple[list[tuple[str, list[Page]]], dict[str, Page]]:
    nav = json.loads((SOURCE / "nav.json").read_text())
    sections: list[tuple[str, list[Page]]] = []
    by_slug: dict[str, Page] = {}
    for sec in nav:
        pages = []
        for slug in sec["pages"]:
            if slug in by_slug:
                raise BuildError(f"nav.json lists {slug} twice")
            if slug == "changelog":
                source, text = CHANGELOG, changelog_source()
            else:
                source = SOURCE / f"{slug}.md"
                if not source.exists():
                    raise BuildError(f"nav.json lists {slug}, but {source.relative_to(ROOT)} does not exist")
                text = source.read_text()
            meta, body = front_matter(text, source)
            page = Page(slug, sec["title"], meta["title"], meta["description"], source)
            page.body = body
            pages.append(page)
            by_slug[slug] = page
        sections.append((sec["title"], pages))
    orphans = {p.stem for p in SOURCE.glob("*.md")} - set(by_slug)
    if orphans:
        raise BuildError(f"pages missing from nav.json: {', '.join(sorted(orphans))}")
    return sections, by_slug


def render(page: Page) -> None:
    md = markdown.Markdown(extensions=EXTENSIONS, extension_configs=EXT_CONFIG)
    out = md.convert(page.body)
    # Tables scroll on their own on small screens; code blocks get a frame.
    out = out.replace("<table>", '<div class="table"><table>').replace("</table>", "</table></div>")
    page.body = out
    page.toc = [t for t in md.toc_tokens]  # type: ignore[attr-defined]
    page.text = re.sub(r"\s+", " ", html.unescape(TAGS.sub(" ", out))).strip()


def anchors(page: Page) -> set[str]:
    return set(re.findall(r'id="([^"]+)"', page.body))


def link(page: Page, by_slug: dict[str, Page]) -> None:
    def repl(m: re.Match) -> str:
        slug, frag = m.group(1), m.group(2) or ""
        target = by_slug.get(slug)
        if target is None:
            raise BuildError(f"{page.source.name}: link to {slug}.md, which is not a page")
        if frag and frag[1:] not in anchors(target):
            raise BuildError(f"{page.source.name}: link to {slug}.md{frag}, which has no such heading")
        return f'href="{target.url}{frag}"'

    page.body = LINK.sub(repl, page.body)
    for frag in re.findall(r'href="#([^"]+)"', page.body):
        if frag not in anchors(page):
            raise BuildError(f"{page.source.name}: link to #{frag}, which has no such heading")


def sprite() -> str:
    """The SVG symbols (seal, icons) of the home page, reused as they are."""
    m = re.search(r'<svg width="0" height="0".*?</svg>', (SITE / "index.html").read_text(), re.S)
    if not m:
        raise BuildError("site/index.html: the SVG sprite was not found")
    return m.group(0)


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def nav_html(sections, current: Page) -> str:
    parts = []
    for title, pages in sections:
        items = "".join(
            f'<li><a href="{p.url}"{" aria-current=\"page\"" if p is current else ""}>{esc(p.title)}</a></li>'
            for p in pages
        )
        parts.append(f'<div class="dnav-sec"><p class="dnav-title">{esc(title)}</p><ul>{items}</ul></div>')
    return "".join(parts)


def toc_html(page: Page) -> str:
    items = []
    for h2 in page.toc:
        sub = "".join(f'<li class="l3"><a href="#{c["id"]}">{esc(html.unescape(c["name"]))}</a></li>' for c in h2.get("children", []))
        items.append(f'<li><a href="#{h2["id"]}">{esc(html.unescape(h2["name"]))}</a></li>{sub}')
    if not items:
        return ""
    return f'<nav class="dtoc" aria-labelledby="toc-title"><p class="dnav-title" id="toc-title">On this page</p><ul>{"".join(items)}</ul></nav>'


def pager(order: list[Page], page: Page) -> str:
    i = order.index(page)
    prev = order[i - 1] if i > 0 else None
    nxt = order[i + 1] if i + 1 < len(order) else None
    cells = []
    if prev:
        cells.append(f'<a class="dpager-prev" href="{prev.url}"><span class="mono">Previous</span><b>{esc(prev.title)}</b></a>')
    if nxt:
        cells.append(f'<a class="dpager-next" href="{nxt.url}"><span class="mono">Next</span><b>{esc(nxt.title)}</b></a>')
    return f'<nav class="dpager" aria-label="Previous and next page">{"".join(cells)}</nav>' if cells else ""


def page_html(page: Page, sections, order: list[Page], icons: str) -> str:
    title = "Kowan documentation" if page.slug == "index" else f"{page.title} · Kowan docs"
    url = BASE_URL + page.url
    crumbs = [{"@type": "ListItem", "position": 1, "name": "Docs", "item": BASE_URL + "/docs/"}]
    if page.slug != "index":
        crumbs.append({"@type": "ListItem", "position": 2, "name": page.title, "item": url})
    ld = json.dumps([
        {"@context": "https://schema.org", "@type": "TechArticle", "headline": page.title,
         "description": page.description, "url": url, "inLanguage": "en",
         "isPartOf": {"@type": "WebSite", "name": "Kowan", "url": BASE_URL + "/"}},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": crumbs},
    ], ensure_ascii=False)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(page.description)}">
<link rel="canonical" href="{url}">
<meta name="color-scheme" content="dark light">
<meta name="theme-color" content="#1a1917" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#fbfaf6" media="(prefers-color-scheme: light)">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Kowan">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(page.description)}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{BASE_URL}/img/og-dashboard.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="preload" href="/fonts/space-grotesk.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/style.css">
<link rel="stylesheet" href="/docs.css">
<script type="application/ld+json">{ld}</script>
</head>
<body class="docs">
{icons}
<a class="skip" href="#content">Skip to content</a>
<header class="top">
  <div class="col top-row">
    <a class="cell brand" href="/" aria-label="Kowan home">
      <svg class="mark" aria-hidden="true"><use href="#kowan-mark"/></svg><span>KOWAN</span>
    </a>
    <a class="cell dtag" href="/docs/">Docs</a>
    <nav class="nav" aria-label="Main">
      <a class="cell wide" href="/#features">Features<svg class="corner" aria-hidden="true"><use href="#i-corner"/></svg></a>
      <a class="cell wide" href="/docs/installation/">Get started<svg class="corner" aria-hidden="true"><use href="#i-corner"/></svg></a>
      <a class="cell hot" href="{REPO}">GitHub<svg class="corner" aria-hidden="true"><use href="#i-corner"/></svg></a>
    </nav>
  </div>
</header>
<div class="col dlayout">
  <aside class="dside">
    <div class="dsearch" role="search">
      <label class="sr-only" for="dsearch-input">Search the documentation</label>
      <input id="dsearch-input" type="search" placeholder="Search the docs" autocomplete="off" spellcheck="false"
             aria-controls="dsearch-results" aria-expanded="false" aria-autocomplete="list">
      <kbd aria-hidden="true">/</kbd>
      <ul id="dsearch-results" class="dsearch-results" role="listbox" aria-label="Search results" hidden></ul>
    </div>
    <details class="dnav-wrap" open>
      <summary class="mono">Menu</summary>
      <nav class="dnav" aria-label="Documentation">{nav_html(sections, page)}</nav>
    </details>
  </aside>
  <main id="content" class="dmain">
    <p class="dcrumb mono"><a href="/docs/">Docs</a><span aria-hidden="true">/</span><span>{esc(page.section)}</span></p>
    <article class="prose">
      <h1>{esc(page.title)}</h1>
      <p class="dlede">{esc(page.description)}</p>
      {page.body}
    </article>
    {pager(order, page)}
    <p class="dedit"><a href="{page.edit_url}">Edit this page on GitHub</a></p>
  </main>
  {toc_html(page)}
</div>
<footer class="dfoot">
  <div class="col"><div class="foot-base"><span>Kowan · Self-hosted Docker management</span><a href="{REPO}">GitHub</a></div></div>
</footer>
<script src="/docs.js" defer></script>
</body>
</html>
"""


def minify_css(text: str) -> str:
    """Comments and layout whitespace out; nothing that changes meaning
    (spaces inside values, around ``+``/``-`` in calc() and before ``:``
    in selectors stay)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([{};,>])\s*", r"\1", text)
    return text.replace(";}", "}").strip() + "\n"


def build() -> list[Page]:
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(SITE, OUT, ignore=shutil.ignore_patterns("_docs"))
    for css in OUT.glob("*.css"):
        css.write_text(minify_css(css.read_text()))
    sections, by_slug = load_pages()
    order = [p for _, pages in sections for p in pages]
    for p in order:
        render(p)
    for p in order:
        link(p, by_slug)
    icons = sprite()
    for p in order:
        target = OUT / p.url.strip("/") / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page_html(p, sections, order, icons))
    index = [
        {"t": p.title, "u": p.url, "s": p.section, "d": p.description,
         "h": [[html.unescape(c["name"]), c["id"]] for t in p.toc for c in [t, *t.get("children", [])]],
         "x": p.text[:6000]}
        for p in order
    ]
    (OUT / "docs" / "search.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")))
    urls = [BASE_URL + "/"] + [BASE_URL + p.url for p in order]
    (OUT / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{u}</loc></url>\n" for u in urls)
        + "</urlset>\n"
    )
    return order


if __name__ == "__main__":
    try:
        pages = build()
    except BuildError as err:
        print(f"build_site: {err}", file=sys.stderr)
        sys.exit(1)
    print(f"built {OUT.relative_to(ROOT)} with {len(pages)} documentation pages")
