"""Static server for a run's tree, with a single-page-app fallback.

`python -m http.server` 404s every path that is not a file, which is wrong for a site
that routes in the browser: `/settings` belongs to the app's router, not to the
filesystem. This server serves files exactly as written, and answers a path that is not a
file or a directory — `/settings`, `/settings/`, `/docs/guides/` — with the tree's
`index.html`, so a client-side route survives a reload instead of dead-ending in a 404.

It is run as a plain script (`python static_server.py <port> --directory <root>`), not
imported, so it works inside a generated workspace with no package on `sys.path`.

The port is a plain HTTP listener, so a request cannot be told apart by headers: any
page on this machine can scan loopback ports. With `--prefix <run nonce>` the tree is
only reachable under that path — every other URL, `/` included, is a 404 — so a run's
files are not readable by guessing a port.
"""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def is_route(path: str, target: Path) -> bool:
    """True when `path` names a client-side route rather than something on disk.

    `/about`, `/about/` and `/docs/guides/` are routes; a real directory is not. The
    harness's own `/preview` route uses this too, so both serve one policy.
    """
    if not path:
        return False
    if target.is_dir():
        return False
    if path.endswith("/"):
        return True
    return not Path(path).suffix


def normalize_prefix(prefix: str) -> str:
    """`abc/`, `/abc` and `/abc/` all mean `/abc`; empty means no prefix."""
    clean = "/" + (prefix or "").strip().strip("/")
    return "" if clean == "/" else clean


class SpaRequestHandler(SimpleHTTPRequestHandler):
    """A file server that hands client-side routes to the app's index.html.

    `prefix` is the run's nonce: with one set, anything not under it is a 404.
    """

    prefix = ""

    def __init__(self, *args, directory=None, prefix="", **kwargs):
        self.prefix = normalize_prefix(prefix)
        super().__init__(*args, directory=directory, **kwargs)

    def send_head(self):
        path = unquote(urlsplit(self.path).path)
        if self.prefix:
            if path == self.prefix:  # exactly the nonce, no trailing slash: send them in
                self.send_response(302)
                self.send_header("Location", f"{self.prefix}/")
                self.end_headers()
                return None
            if not path.startswith(self.prefix + "/"):
                self.send_error(404, "not found")
                return None
            # Strip the nonce before mapping the rest onto the tree.
            self.path = path[len(self.prefix) :] or "/"
            path = self.path
        if is_route(path, Path(self.translate_path(path))):
            self.path = "/index.html"
        return super().send_head()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a static tree with SPA route fallback.")
    parser.add_argument("port", type=int)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--directory", default=".")
    parser.add_argument("--prefix", default="", help="Serve only under this path; everything else is a 404.")
    args = parser.parse_args(argv)

    handler = partial(SpaRequestHandler, directory=args.directory, prefix=args.prefix)
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
