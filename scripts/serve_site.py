"""Build the website and serve it locally at the root, the same path GitHub
Pages uses on kowan.io. Edits under site/ (pages, docs, styles) or to
docs/CHANGELOG.md rebuild it on the next request.

    pip install -r requirements-site.txt
    python3 scripts/serve_site.py [port]    # default 8792

Then open http://localhost:8792/ . Missing paths get the site's 404 page, like
on Pages.
"""

import functools
import http.server
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import build_site  # noqa: E402

WATCH = [build_site.SITE, build_site.CHANGELOG, pathlib.Path(build_site.__file__)]
_lock = threading.Lock()
_built_at = 0.0


def _newest() -> float:
    newest = 0.0
    for root in WATCH:
        paths = [root] if root.is_file() else root.rglob("*")
        for p in paths:
            if p.is_file():
                newest = max(newest, p.stat().st_mtime)
    return newest


def rebuild_if_changed() -> None:
    global _built_at
    with _lock:
        newest = _newest()
        if newest <= _built_at:
            return
        try:
            pages = build_site.build()
            print(f"rebuilt with {len(pages)} documentation pages", flush=True)
        except build_site.BuildError as err:
            print(f"build failed: {err}", flush=True)
        _built_at = newest


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        rebuild_if_changed()
        super().do_GET()

    def do_HEAD(self) -> None:
        rebuild_if_changed()
        super().do_HEAD()

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        if code != 404:
            super().send_error(code, message, explain)
            return
        body = (build_site.OUT / "404.html").read_bytes()
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8792
    rebuild_if_changed()
    handler = functools.partial(Handler, directory=str(build_site.OUT))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as srv:
        print(f"Serving {build_site.OUT} at http://localhost:{port}/", flush=True)
        srv.serve_forever()
