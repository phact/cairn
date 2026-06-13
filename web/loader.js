/* ============================================================================
   Cairn — trace loader. LIVE ONLY.

   The UI is driven exclusively by the backend query API. There is no bundled
   demo and no fallback: if the live trace cannot be loaded, the app shows an
   explicit error rather than inventing or substituting data.

   Config (all optional, via URL):
     ?api=<base>        backend base (default /api, proxied by serve.py)
     ?scenario=<id>     scenario id (default below; self-heals from the API's
                        404 `have` field if stale)
     ?threshold=<0..1>  skeleton salience cutoff (default 0.6)

   Exposes window.cairnLoadTrace() -> Promise<{
     trace, apiBase, scenarioId, threshold, error
   }>  — on failure, trace is null and error is set.
   ========================================================================== */
(function () {
  "use strict";

  var DEFAULT_API = "/api";

  function config() {
    var q = new URLSearchParams(window.location.search);
    return {
      apiBase: (q.get("api") || window.CAIRN_API || DEFAULT_API).replace(/\/$/, ""),
      // No hardcoded default — the active trace is discovered from GET /status.
      // An explicit ?scenario= (or window.CAIRN_SCENARIO) overrides discovery.
      scenarioOverride: q.get("scenario") || window.CAIRN_SCENARIO || null,
      scenarioId: null,
      threshold: q.get("threshold") || "0.6",
    };
  }

  function url(base, sid, suffix) {
    // sid carries "::" — a legal path segment (no slashes) the backend matches
    // verbatim, so it must NOT be percent-encoded.
    return base + "/scenario/" + sid + (suffix || "");
  }

  function getJSON(u) {
    return fetch(u, { headers: { Accept: "application/json" } }).then(function (r) {
      return r.text().then(function (txt) {
        var data;
        try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = null; }
        if (!r.ok) {
          var err = new Error((data && data.error) ? data.error : ("HTTP " + r.status));
          err.status = r.status;
          err.body = data;
          throw err;
        }
        return data;
      });
    });
  }

  // Pick the scenario to load: an explicit override, else the backend's active
  // trace from GET /status. No hardcoding — tracks whatever the backend serves.
  function pickScenario(cfg) {
    if (cfg.scenarioOverride) return Promise.resolve(cfg.scenarioOverride);
    return getJSON(cfg.apiBase + "/status").then(function (s) {
      if (!s || !s.active_trace) throw new Error("no active trace");
      return s.active_trace;
    });
  }

  // Fetch the artifact; if the id turns out stale, the 404 carries the truth
  // (active_trace, and a `have` list) — adopt it and retry once.
  function resolveScenario(cfg) {
    return getJSON(url(cfg.apiBase, cfg.scenarioId, "")).catch(function (err) {
      if (err.status === 404 && err.body) {
        var alt = err.body.active_trace ||
          (Array.isArray(err.body.have) ? err.body.have[0] : err.body.have) || null;
        if (alt && alt !== cfg.scenarioId) {
          cfg.scenarioId = alt;
          return getJSON(url(cfg.apiBase, cfg.scenarioId, ""));
        }
      }
      throw err;
    });
  }

  function cairnLoadTrace() {
    var cfg = config();
    if (!window.CairnAdapter) {
      return Promise.resolve({ trace: null, error: "adapter.js not loaded", apiBase: cfg.apiBase, scenarioId: cfg.scenarioId, threshold: cfg.threshold });
    }
    return pickScenario(cfg).then(function (sid) {
      cfg.scenarioId = sid;
      return resolveScenario(cfg);
    }).then(function (art) {
      return getJSON(url(cfg.apiBase, cfg.scenarioId, "/skeleton?threshold=" + cfg.threshold))
        .then(function (skel) {
          var trace = window.CairnAdapter.adaptArtifact(art, skel);
          if (!trace.frames.length) throw new Error("backend returned no salient frames");
          return { trace: trace, apiBase: cfg.apiBase, scenarioId: cfg.scenarioId, threshold: cfg.threshold, error: null };
        });
    }).catch(function (err) {
      return { trace: null, apiBase: cfg.apiBase, scenarioId: cfg.scenarioId, threshold: cfg.threshold, error: String((err && err.message) || err) };
    });
  }

  window.cairnLoadTrace = cairnLoadTrace;
})();
