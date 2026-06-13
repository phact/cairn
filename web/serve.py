#!/usr/bin/env python3
"""
Cairn frontend dev server.

Serves the static UI AND reverse-proxies /api/* to the backend query API, so the
browser talks to a single origin (no CORS, and no edits to the backend — which a
separate agent owns). React + Babel load from a CDN and JSX transpiles in the
browser, so there is still no build step.

  /            -> static files in this directory (index.html, *.jsx, *.css, …)
  /api/<path>  -> <CAIRN_API>/<path>   (default backend http://127.0.0.1:8787)

Env:
  CAIRN_WEB_PORT   port to serve the UI on        (default 8123)
  CAIRN_API        backend base URL to proxy to   (default http://127.0.0.1:8787)
"""

import os
import sys
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.environ.get("CAIRN_API", "http://127.0.0.1:8787").rstrip("/")
PORT = int(os.environ.get("CAIRN_WEB_PORT", "8123"))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=WEB_DIR, **k)

    def log_message(self, *a):
        pass

    def _is_api(self):
        return self.path == "/api" or self.path.startswith("/api/")

    def _proxy(self, method):
        # /api/scenario/foo/skeleton?x=1  ->  <BACKEND>/scenario/foo/skeleton?x=1
        upstream = BACKEND + self.path[len("/api"):]
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(upstream, data=body, method=method)
        ctype = self.headers.get("Content-Type")
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except urllib.error.URLError as e:
            # backend down / in flux — report 502 so the UI can fall back to the
            # bundled mock instead of hanging.
            msg = ('{"error":"backend unreachable","detail":%r,"backend":%r}'
                   % (str(e.reason), BACKEND)).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_GET(self):
        if self._is_api():
            return self._proxy("GET")
        return super().do_GET()

    def do_POST(self):
        if self._is_api():
            return self._proxy("POST")
        self.send_error(405, "POST only supported under /api")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Cairn UI (live-only) on http://127.0.0.1:%d" % PORT)
    print("  proxying /api/* -> %s" % BACKEND)
    print("  needs the backend (cairn/api.py) up, or the UI shows 'No live trace'")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
