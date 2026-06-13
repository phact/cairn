/* ============================================================================
   Cairn — backend→view adapter.

   Maps the documented trace-artifact contract (spec.md / cairn/api.py) onto the
   shape the React views consume (mirrored by data.js). Coded against the STABLE
   contract, not today's output, so it survives the backend changing under it.

   Honesty rule (spec's governing principle): this layer never invents executed
   facts. Where the contract is thinner than the demo mock — source text, predict
   authoring — it DERIVES presentation from real recorded fields (taken arm,
   not-taken arm, deciding value) or degrades visibly, but it never fabricates
   what ran.

   Two pure functions, exposed on window.CairnAdapter:
     adaptArtifact(artifact, skeleton) -> view-shaped TRACE
     adaptLoc(locResponse)             -> { lines:[{n, locals}], reverse, note }
   ========================================================================== */
(function () {
  "use strict";

  var SRC_PREFIX = /^crates\/[^/]+\/src\//;          // crates/<crate>/src/oauth/x.rs -> oauth/x.rs
  var KNOWN_EVENTS = { read: 1, born: 1, mutated: 1 };
  var VAL_MAX = 140;

  function shortFile(p) {
    if (!p) return "";
    return String(p).replace(SRC_PREFIX, "");
  }
  function leaf(id) { return String(id).split("::").pop(); }
  function displayModule(fileId) { return String(fileId).replace(/\.rs$/, ""); } // oauth/middleware.rs -> oauth/middleware
  function truncVal(v) {
    var s = v == null ? "" : String(v);
    return s.length > VAL_MAX ? s.slice(0, VAL_MAX) + "…" : s;
  }
  // Display-only: collapse Rust module qualifiers on type paths the way a
  // debugger pretty-printer does — alloc::vec::Vec<core::net::…::SocketAddr,
  // alloc::alloc::Global> -> Vec<SocketAddr, Global>. Purely cosmetic; the full
  // recorded string is kept in `raw` and shown on hover, so no value is lost.
  function prettyType(v) {
    return String(v == null ? "" : v)
      .replace(/(?:[a-z_][A-Za-z0-9_]*::)+([A-Za-z_][A-Za-z0-9_]*)/g, "$1");
  }
  function valDisplay(v) { return truncVal(prettyType(v)); }   // compact (inline chips)
  function valFull(v) {                                          // full, for the state rail + hover
    var s = prettyType(v);
    return s.length > 1200 ? s.slice(0, 1200) + "…" : s;
  }
  function normEvent(e) { return KNOWN_EVENTS[e] ? e : "read"; }

  // "is_valid == true" / "upsert ⇒ Ok(..)" / "x = y" -> { name, value }
  function parseDeciding(s) {
    if (s == null) return { name: "", value: "" };
    if (typeof s === "object") {
      return { name: s.name != null ? String(s.name) : "", value: s.value != null ? String(s.value) : "" };
    }
    var str = String(s);
    var seps = ["==", "⇒", "=>", "!=", ">=", "<=", "="];
    for (var i = 0; i < seps.length; i++) {
      var idx = str.indexOf(seps[i]);
      if (idx > 0) {
        return { name: str.slice(0, idx).trim(), value: truncVal(str.slice(idx + seps[i].length).trim()) };
      }
    }
    return { name: truncVal(str), value: "" };
  }

  function parseMs(detail) {
    var m = /(\d+)\s*ms/.exec(detail || "");
    return m ? parseInt(m[1], 10) : null;
  }

  // Build function-granularity lanes (Y axis) from the file-level lanes + their
  // children, restricted to the functions the walked frames actually touch.
  // Order is the contract's source order: (file.source_order, child index).
  function buildLanes(artLanes, frames) {
    var fileById = {};
    (artLanes || []).forEach(function (l) { fileById[l.id] = l; });

    var used = [];
    var seen = {};
    frames.forEach(function (f) { if (!seen[f.lane]) { seen[f.lane] = 1; used.push(f.lane); } });

    var rows = used.map(function (laneId) {
      var fileId = laneId.indexOf("::") >= 0 ? laneId.slice(0, laneId.indexOf("::")) : laneId;
      var fl = fileById[fileId];
      var childIdx = fl && fl.children ? fl.children.indexOf(laneId) : -1;
      var external = (fl && /^::/.test(fl.module || "")) || /^::/.test(fileId);
      return {
        id: laneId,
        module: external ? (fl ? fl.module : fileId) : displayModule(fileId),
        file: external ? null : fileId,
        kind: "function",
        label: laneId,
        short: leaf(laneId),
        external: external,
        _fileOrder: fl ? (fl.source_order != null ? fl.source_order : 999) : 999,
        _childIdx: childIdx < 0 ? 999 : childIdx,
      };
    });

    rows.sort(function (a, b) { return (a._fileOrder - b._fileOrder) || (a._childIdx - b._childIdx); });
    rows.forEach(function (r, i) { r.source_order = i; delete r._fileOrder; delete r._childIdx; });
    return rows;
  }

  function adaptBranch(b, frame) {
    if (!b) return null;
    var dv = parseDeciding(b.deciding_value);
    var nt = b.not_taken || {};
    var ntArm = nt.arm || "other arm";
    var ntSummary = nt.summary || "the path that did not run";
    var ntLine = nt.target_line != null ? nt.target_line : null;
    return {
      taken_arm: b.taken_arm || "taken",
      deciding_value: dv,
      surprising: !!b.surprising,
      // predict authoring synthesized ONLY from real recorded arms — the taken
      // arm is ground truth, so `correct` is grounded, not guessed.
      prompt: "Surprising fork — which arm actually fired?",
      candidates: [
        { id: "taken", label: b.taken_arm || "taken arm", line: frame.line, hint: "what actually ran" },
        { id: "not_taken", label: ntArm, line: ntLine, hint: ntSummary },
      ],
      correct: "taken",
      reveal: "Took: " + (b.taken_arm || "the taken arm") +
              ". Decided by " + dv.name + (dv.value ? " = " + dv.value : "") +
              ". The other arm (" + ntArm + ") " + ntSummary + ".",
      not_taken: { arm: ntArm, target_line: ntLine, summary: ntSummary },
    };
  }

  function adaptFrame(f, denseStep, boundaryByStep) {
    var file = shortFile(f.file);
    var out = {
      id: f.id,
      lane: f.lane,
      step: denseStep,                 // dense X index for layout (orig step is sparse)
      kind: f.kind,
      file: file,
      fullFile: f.file,                // unshortened path, for /source?path=
      line: f.line,
      salience: f.salience,
      role: f.role || leaf(f.lane),
      values: (f.values || []).map(function (v) {
        return { name: v.name, value: valDisplay(v.value), full: valFull(v.value), ref: v.ref || null, tree: v.tree || null, as_time: v.as_time || null, event: normEvent(v.event) };
      }),
      loop_count: f.loop_count != null ? f.loop_count : null,
      // No source text in the contract — the LOC view degrades honestly rather
      // than inventing code. Real executed lines come lazily from /…/loc.
      code: { fn: leaf(f.lane), file: file, start: f.line, lines: [], sourceless: true },
    };
    if (f.kind === "io") {
      var b = boundaryByStep[f._origStep];
      out.io = {
        detail: b ? b.detail : (f.role || f.lane),
        lane: b ? b.lane : f.lane,
        waited_ms: b ? parseMs(b.detail) : null,
      };
    }
    if (f.branch) out.branch = adaptBranch(f.branch, f);
    return out;
  }

  function adaptArtifact(artifact, skeleton) {
    var art = artifact || {};
    // The walkable path = the ranked skeleton (one representative per lane),
    // sorted into logical step order. Falls back to all frames if no skeleton.
    var raw = (skeleton && skeleton.frames && skeleton.frames.length)
      ? skeleton.frames.slice()
      : (art.frames || []).slice();
    raw.sort(function (a, b) { return a.step - b.step; });
    raw.forEach(function (f) { f._origStep = f.step; });

    var boundaryByStep = {};
    (art.boundaries || []).forEach(function (b) { boundaryByStep[b.step] = b; });

    var frames = raw.map(function (f, i) { return adaptFrame(f, i, boundaryByStep); });

    var lanes = buildLanes(art.lanes, frames);

    // Re-derive boundaries from the kept I/O frames so the swimlane's seams line
    // up with the dense X axis (original boundary steps index the full 221-frame
    // space, not this collapsed path).
    var boundaries = frames
      .filter(function (f) { return f.kind === "io"; })
      .map(function (f) {
        return { step: f.step, kind: "io", detail: (f.io && f.io.detail) || f.role, lane: f.lane };
      });

    var sc = art.scenario || {};
    var lastSeg = leaf(sc.id || "scenario");
    var scenario = {
      id: sc.id || "scenario",
      crate: sc.crate || "",
      capture_backend: sc.capture_backend || "rr-dwarf",
      title: lastSeg,
      subtitle: "recorded execution · " + frames.length + " salient frames",
    };

    // Prose. Rendered verbatim as a paragraph — the view never rewrites the
    // backend's summary (mock data.js still ships a clause array, kept as-is).
    var precis = Array.isArray(art.precis) ? art.precis : String(art.precis || "");

    return {
      scenario: scenario,
      lanes: lanes,
      frames: frames,
      causal_edges: (skeleton && skeleton.causal_edges) || art.causal_edges || [],
      boundaries: boundaries,
      precis: precis,
    };
  }

  // Tier-3 LOC response (executed line numbers + DWARF locals). No source text
  // in the contract, so we surface lines + their recorded locals as-is.
  function adaptLoc(loc) {
    if (!loc) return null;
    var lines = (loc.lines || []).map(function (ln) {
      return {
        n: ln.line,
        text: ln.text != null ? ln.text : null,   // real source, when the backend serves it
        locals: (ln.locals || []).map(function (v) {
          return { name: v.name, value: valDisplay(v.value), full: valFull(v.value), ref: v.ref || null, tree: v.tree || null, kind: v.kind || "local" };
        }),
      };
    });
    var hasSource = lines.some(function (l) { return l.text != null; });
    var rev = loc.reverse_step || {};
    return {
      function: loc.function,
      file: shortFile(loc.file),
      entry_line: loc.entry_line,
      sourcePath: loc.source_path || null,
      hasSource: hasSource,
      lines: lines,
      reverse: rev.supported ? {
        instant: rev.reread_instant,
        line: rev.reread_line,
        matches: !!rev.matches_forward,
        locals: (rev.reread_locals || []).map(function (v) {
          return { name: v.name, value: truncVal(v.value), kind: v.kind || "local" };
        }),
      } : null,
      note: hasSource
        ? "Real source lines from the recording, with DWARF-decoded locals read at each line."
        : "Executed line numbers and DWARF-decoded locals from the recording. " +
          "Source text is not part of the trace contract, so it is not shown here.",
    };
  }

  window.CairnAdapter = {
    adaptArtifact: adaptArtifact,
    adaptLoc: adaptLoc,
    _internal: { parseDeciding: parseDeciding, shortFile: shortFile, buildLanes: buildLanes },
  };
})();
