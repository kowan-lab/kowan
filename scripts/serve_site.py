"""Serve site/ locally under /kowan/, the same path GitHub Pages uses.

    python3 scripts/serve_site.py [port]    # default 8792

Then open http://localhost:8792/kowan/ . Paths outside /kowan/ get the site's
404 page, like on Pages.
"""

import functools
import http.server
import pathlib
import sys

SITE = pathlib.Path(__file__).resolve().parent.parent / "site"
PREFIX = "/kowan"


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        p = path.split("?", 1)[0].split("#", 1)[0]
        if p == PREFIX:
            p = PREFIX + "/"
        if not p.startswith(PREFIX + "/"):
            return str(SITE / "404.html")
        return super().translate_path(p[len(PREFIX):])

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == PREFIX:
            self.send_response(301)
            self.send_header("Location", PREFIX + "/")
            self.end_headers()
            return
        super().do_GET()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8792
    handler = functools.partial(Handler, directory=str(SITE))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as srv:
        print(f"Serving {SITE} at http://localhost:{port}{PREFIX}/", flush=True)
        srv.serve_forever()
