#!/usr/bin/env bash
# Serve the Cairn frontend. Static files only — React + Babel load from a CDN
# and the JSX is transpiled in the browser, so there is no build step.
# The mock trace (data.js) mirrors the backend artifact contract; to drive the
# UI from a live recording, replace data.js's window.CAIRN_TRACE with the
# artifact served by `cairn/api.py` (GET /scenario/:id).
#   - mock demo (default):  http://127.0.0.1:8123/
#   - live trace:           http://127.0.0.1:8123/?live   (needs cairn/api.py up)
# serve.py also reverse-proxies /api/* to the backend (CAIRN_API, default
# http://127.0.0.1:8787) so the browser stays same-origin — no CORS, no backend
# edits.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 serve.py
