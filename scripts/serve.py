#!/usr/bin/env python3
"""Serve the font collection so the browser UI can render the fonts themselves.

Roots at the collection folder (the folder holding this repo) so the app lives
at /<repo>/web/ and every font resolves at its manifest path, e.g.
/A/Agenda/Agenda-Black.otf. Adds the font MIME types http.server doesn't know,
gzips the manifest on the fly, and threads requests so the dozens of parallel
font loads a scrolling list kicks off don't queue behind each other.

Usage:
    python3 serve.py [-p PORT] [-b ADDRESS]
"""

import argparse
import gzip
import io
import os
import posixpath
import sys
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# scripts/ lives in the repo, the repo lives in the collection
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
COLLECTION_DIR = os.path.dirname(REPO_DIR)
APP_PATH = f"/{os.path.basename(REPO_DIR)}/web/"

FONT_TYPES = {
    ".otf": "font/otf",
    ".ttf": "font/ttf",
    ".ttc": "font/collection",
    ".otc": "font/collection",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".eot": "application/vnd.ms-fontobject",
    ".json": "application/json",
    ".mjs": "text/javascript",
}


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, **FONT_TYPES}

    def send_head(self):
        """Gzip the manifest; everything else goes through the default path."""
        path = self.translate_path(self.path)
        if (
            path.endswith(".json")
            and os.path.isfile(path)
            and "gzip" in self.headers.get("Accept-Encoding", "")
        ):
            with open(path, "rb") as fh:
                body = gzip.compress(fh.read(), 6)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return io.BytesIO(body)
        return super().send_head()

    def end_headers(self):
        # the app and manifest change under you; fonts never do
        if posixpath.splitext(self.path)[1].lower() in (
            ".otf",
            ".ttf",
            ".woff",
            ".woff2",
            ".ttc",
            ".otc",
            ".eot",
            ".svg",
        ):
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if "-v" in sys.argv or "--verbose" in sys.argv:
            super().log_message(fmt, *args)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-p", "--port", type=int, default=8000)
    ap.add_argument("-b", "--bind", default="127.0.0.1")
    ap.add_argument(
        "-d",
        "--directory",
        default=COLLECTION_DIR,
        help="collection root (default: the folder holding this repo)",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="log requests")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = ap.parse_args()

    root = os.path.abspath(args.directory)
    if not os.path.isdir(root):
        sys.exit(f"not a directory: {root}")

    manifest = os.path.join(REPO_DIR, "web", "manifest.json")
    if not os.path.exists(manifest):
        print("warning: no manifest yet - run build-manifest.py first", file=sys.stderr)

    os.chdir(root)
    url = f"http://{args.bind}:{args.port}{APP_PATH}"
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True
    print(f"serving {root}\n{url}\nctrl-c to stop")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
