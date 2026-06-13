# Cairn — frontend (live-only)

The UI that consumes the trace artifact. No build step: React + Babel load from a
CDN and the JSX transpiles in the browser. **There is no demo or mock data** —
the UI shows a real recorded execution from the backend, or an explicit
"No live trace" error. It never substitutes or invents data.

## Run

```
# 1. start the backend query API (separate component)
CAIRN_PORT=8787 python3 ../cairn/api.py

# 2. serve the UI
./serve.sh                      # http://127.0.0.1:8123
```

`serve.py` reverse-proxies `/api/*` to the backend, so the browser stays
same-origin (no CORS, no edits to the backend). Override the backend with
`CAIRN_API=http://host:port ./serve.sh`.

URL params: `?api=<base>`, `?scenario=<id>`, `?threshold=<0..1>`. The scenario id
**self-heals**: if the configured id is stale, the loader adopts the id from the
API's 404 `have` field, so backend renames don't break loading.

## How the wiring works

The views were authored against a richer shape than the documented artifact
contract. Two layers bridge the gap, coded against the **stable contract**:

- `adapter.js` — pure transform from the artifact + skeleton (`/scenario/:id`,
  `/scenario/:id/skeleton`) to the view shape:
  - expands file-level lanes into the function-level lanes the frames reference,
    in stable source order;
  - re-indexes sparse logical steps to a dense X axis;
  - parses the `deciding_value` string into `{name, value}`;
  - synthesizes the predict prompt **only from real recorded arms** (the taken
    arm is ground truth, so the "correct" answer is grounded, not guessed).
- `loader.js` — fetches + adapts the live trace; on failure returns an error
  (the app renders it; no fallback).

**Précis** is the backend's prose string, rendered verbatim. **Tier 3 (LOC)** has
no source text in the contract; it is replayed on demand
(`/scenario/:id/frame/:fid/loc`) and rendered as executed line numbers +
DWARF-decoded locals (plus rr reverse-step), never invented source. Recorded
values are shown verbatim (length-capped only).

Files: `index.html`, `app.jsx` (orchestrator + error screen), `spine.jsx` /
`precis.jsx` / `skeleton.jsx` / `loc.jsx` / `predict.jsx` (views),
`tweaks-panel.jsx`, `styles.css` / `views.css`, `adapter.js` + `loader.js` (live
wiring), `serve.py` (static + proxy).
