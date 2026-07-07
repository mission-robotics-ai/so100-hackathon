"""The hackathon course site: rerun.io/learn-style pages rendered from ``web/content/*.md``.

Dev server (re-renders on every request, so edits show on reload)::

    pixi run learn            # serves http://localhost:3000 and opens it

Static export (plain HTML + /static + /viewer, deployable to any static host)::

    pixi run learn -- --build out/

The site is fully decoupled from the data server: pages embed the Rerun web viewer
(assets served same-origin under ``/viewer/``), and ``web/static/app.js`` connects it to
the local ``pixi run so100-server`` (gRPC proxy :9876, control API :8000). When that
server is down the widgets show a hint and keep retrying -- the site never owns
hardware or data itself.

Layout: horizontal stepper on top (condensing on scroll, ``web/static/learn2.js``), wide
content, ``web/static/learn2.css`` layered over ``learn.css``. Old ``/v2/...`` URLs
redirect to the same page at the plain path.
"""

from __future__ import annotations

import dataclasses
import html
import shutil
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import markdown
import tyro

from so100_hackathon.console import info, success
from so100_hackathon.web_assets import VIEWER_ROUTES, ensure_web_viewer_assets

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTENT_DIR = REPO_ROOT / "web" / "content"
STATIC_DIR = REPO_ROOT / "web" / "static"

COURSE_TITLE = "Embodied Metal Hackathon"

MIME_TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".png": "image/png"}

CHECK_SVG = (
    '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"'
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5L20 7"/></svg>'
)


@dataclasses.dataclass(frozen=True)
class Page:
    """One course article, parsed from ``web/content/<slug>.md``."""

    slug: str
    title: str
    order: int
    body_md: str

    @property
    def verb(self) -> str:
        """``"Collect: record episodes"`` -> ``"Collect"`` (stepper name)."""
        return self.title.split(": ", 1)[0]


def _parse_front_matter(text: str) -> tuple[dict[str, str | list[str]], str]:
    """Parse the leading ``---`` block: ``key: value`` scalars + ``- item`` lists."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("content file is missing its `---` frontmatter block")
    meta: dict[str, str | list[str]] = {}
    list_key: str | None = None
    for i, line in enumerate(lines[1:], start=1):
        stripped = line.strip()
        if stripped == "---":
            return meta, "\n".join(lines[i + 1 :])
        if stripped.startswith("- ") and list_key is not None:
            items = meta[list_key]
            assert isinstance(items, list)
            items.append(stripped[2:].strip().strip('"'))
        elif ":" in stripped:
            key, _, value = stripped.partition(":")
            key, value = key.strip(), value.strip().strip('"')
            if value:
                meta[key] = value
                list_key = None
            else:
                meta[key] = []
                list_key = key
    raise ValueError("frontmatter block is not closed with `---`")


def load_pages(content_dir: Path = CONTENT_DIR) -> list[Page]:
    pages: list[Page] = []
    for path in sorted(content_dir.glob("*.md")):
        meta, body = _parse_front_matter(path.read_text(encoding="utf-8"))
        pages.append(
            Page(
                slug=path.stem,
                title=str(meta.get("title", path.stem)),
                order=int(str(meta.get("order", 0))),
                body_md=body,
            )
        )
    pages.sort(key=lambda page: page.order)
    return pages


# --- rendering ------------------------------------------------------------------


def render_stepper(pages: list[Page], current_order: int) -> str:
    """The horizontal stepper: one dot per step across the top, done/current/upcoming.

    The first page (the welcome homepage, reached via the site title) is not a step.
    """
    parts = ['<ol class="stepper">']
    for i, page in enumerate(pages[1:]):
        state = "done" if page.order < current_order else ("current" if page.order == current_order else "upcoming")
        line = "" if i == 0 else f'<span class="step-line{" filled" if page.order <= current_order else ""}" aria-hidden="true"></span>'
        aria = ' aria-current="page"' if state == "current" else ""
        parts.append(
            f'<li>{line}<a class="step" data-state="{state}" href="/{page.slug}/"{aria}>'
            f'<span class="step-dot">{CHECK_SVG if state == "done" else ""}</span>'
            f'<span class="step-name">{html.escape(page.verb)}</span></a></li>'
        )
    parts.append("</ol>")
    return "".join(parts)


def _shell(*, title: str, nav_html: str, article_html: str) -> str:
    header = (
        f'<header class="site-header"><a class="site-title" href="/">{html.escape(COURSE_TITLE)}</a>\n'
        '<span class="site-sub">the data collection loop</span></header>'
    )
    # Header + stepper share one sticky wrapper so the condensed state (learn2.js)
    # can slide the header away and dock the stepper with a single transform.
    chrome = f'<div class="chrome">\n{header}\n<nav class="stepper-nav" aria-label="Course steps">{nav_html}</nav>\n</div>\n<div class="layout">'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap">
<link rel="stylesheet" href="/static/learn.css">
<link rel="stylesheet" href="/static/learn2.css">
</head>
<body class="v2">
{chrome}
<article class="content">{article_html}</article>
</div>
<script type="module" src="/static/app.js"></script>
<script type="module" src="/static/learn2.js"></script>
</body>
</html>"""


def render_page(pages: list[Page], page: Page) -> str:
    index = pages.index(page)
    prev_page = pages[index - 1] if index > 0 else None
    next_page = pages[index + 1] if index + 1 < len(pages) else None

    prev_html = (
        f'<a class="pager-link" href="/{prev_page.slug}/"><span class="pager-dir">&larr; Previous</span><span>{html.escape(prev_page.title)}</span></a>'
        if prev_page
        else '<span class="pager-link"></span>'
    )
    next_html = (
        f'<a class="pager-link pager-next" href="/{next_page.slug}/"><span class="pager-dir">Next &rarr;</span><span>{html.escape(next_page.title)}</span></a>'
        if next_page
        else '<span class="pager-link pager-done">That&rsquo;s the whole loop &mdash; go collect great data!</span>'
    )

    body_html = markdown.markdown(page.body_md, extensions=["fenced_code", "tables"])
    article = f"""
<h1>{html.escape(page.title)}</h1>
{body_html}
<footer class="pager">{prev_html}{next_html}</footer>"""
    return _shell(title=f"{page.title} - {COURSE_TITLE}", nav_html=render_stepper(pages, page.order), article_html=article)


# --- serving / building ---------------------------------------------------------


def make_handler(viewer_assets: dict[str, Path]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # silence request logging
            pass

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in VIEWER_ROUTES:
                asset, content_type = VIEWER_ROUTES[path]
                self._send(200, content_type, viewer_assets[asset].read_bytes())
                return
            if path.startswith("/static/"):
                file = STATIC_DIR / Path(path).name  # flat dir; Path().name blocks traversal
                if file.is_file():
                    self._send(200, MIME_TYPES.get(file.suffix, "application/octet-stream"), file.read_bytes())
                else:
                    self._send(404, "text/plain", b"not found")
                return
            # This layout used to live under /v2/ while the old one held the plain URLs:
            # send stale tabs/bookmarks to the same page at its real path.
            if path == "/v2" or path.startswith("/v2/"):
                self.send_response(301)
                self.send_header("Location", path.removeprefix("/v2") or "/")
                self.end_headers()
                return
            pages = load_pages()  # re-read every request: live-edit the markdown
            if path == "/":  # no cover page: land straight on the first article
                self._send(200, "text/html; charset=utf-8", render_page(pages, pages[0]).encode())
                return
            slug = path.strip("/")
            page = next((p for p in pages if p.slug == slug), None)
            if page is None:
                self._send(404, "text/plain", b"not found")
            else:
                self._send(200, "text/html; charset=utf-8", render_page(pages, page).encode())

    return Handler


def build_static(out_dir: Path) -> None:
    pages = load_pages()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_page(pages, pages[0]), encoding="utf-8")
    for page in pages:
        page_dir = out_dir / page.slug
        page_dir.mkdir(exist_ok=True)
        (page_dir / "index.html").write_text(render_page(pages, page), encoding="utf-8")
    shutil.copytree(STATIC_DIR, out_dir / "static", dirs_exist_ok=True)
    viewer_assets = ensure_web_viewer_assets()
    viewer_dir = out_dir / "viewer"
    viewer_dir.mkdir(exist_ok=True)
    for route, (asset, _content_type) in VIEWER_ROUTES.items():
        shutil.copyfile(viewer_assets[asset], out_dir / route.lstrip("/"))
    success(f"built {len(pages)} pages -> {out_dir}/ (plus /static and /viewer)")


@dataclasses.dataclass
class Config:
    port: int = 3000
    """Dev-server port."""

    open_browser: bool = True
    """Open http://localhost:<port> once the server is up."""

    build: Path | None = None
    """Instead of serving, write the fully static site to this folder and exit."""


def main(config: Config) -> None:
    if config.build is not None:
        build_static(config.build)
        return
    viewer_assets = ensure_web_viewer_assets()
    load_pages()  # fail fast on broken frontmatter
    httpd = ThreadingHTTPServer(("localhost", config.port), make_handler(viewer_assets))
    url = f"http://localhost:{config.port}"
    info(f"course site:        {url}  (Ctrl-C to stop; edits to web/content/*.md show on reload)")
    if config.open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        info("\nshutting down")


if __name__ == "__main__":
    main(tyro.cli(Config))
