/* Cairn — app orchestrator.
   Persistent spine + semantic-zoom router across three tiers, the predict
   loop, and the frontier model that drives the understanding map. */

const { useState, useEffect, useRef, useMemo, useCallback } = React;
// Live only. The trace is fetched from the backend at bootstrap and stashed
// here before the first render; on failure the app shows an error, never a
// substitute. See the bottom of this file.
let TRACE = null;
let SOURCE = { apiBase: null, scenarioId: null };
const TIER_RANK = { precis: 0, skeleton: 1, loc: 2 };
const KINDS = ["call", "branch", "io", "loop_collapsed", "return"];

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "dark",
  "accent": "#F2A93B",
  "animSpeed": 1
}/*EDITMODE-END*/;

function initModel() {
  const m = {};
  KINDS.forEach((k) => (m[k] = { confidence: 0, status: "cold", seen: 0 }));
  return m;
}

// absorb a frame on first visit — non-branch kinds chart passively (never in
// doubt); branches only really move when tested by a prediction.
function absorbModel(model, frame) {
  const m = { ...model };
  const k = frame.kind;
  const e = { ...m[k] };
  e.seen += 1;
  if (k === "branch") {
    if (!frame.branch.surprising) e.confidence = Math.min(0.5, e.confidence + 0.18);
    if (e.status === "cold" && e.confidence > 0) e.status = "cold";
  } else {
    e.confidence = Math.min(1, e.confidence + 0.5);
    if (e.confidence >= 0.55) e.status = "quiet";
  }
  m[k] = e;
  return m;
}

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [tier, setTier] = useState("loc");      // entry = depth, not the swimlane
  const [dir, setDir] = useState("in");
  const [frameIdx, setFrameIdx] = useState(0);
  const [collapsed, setCollapsed] = useState(new Set());
  const [narrateOnly, setNarrateOnly] = useState(false);
  const [model, setModel] = useState(() => absorbModel(initModel(), TRACE.frames[0]));
  const [predictLog, setPredictLog] = useState({}); // frameId -> {choice, correct, declined}
  const [skipPredict, setSkipPredict] = useState(new Set());
  const [paneKey, setPaneKey] = useState(0);
  const [showConfig, setShowConfig] = useState(false);
  const [precisSel, setPrecisSel] = useState(0);   // selected highlight on the summary page
  const [locData, setLocData] = useState({}); // live tier-3: frameId -> {status, data?, error?}
  const visited = useRef(new Set([TRACE.frames[0].id]));

  const frame = TRACE.frames[frameIdx];

  // Tier 3 (LOC) has no source text in the artifact — it is replayed on demand.
  // Fetch executed lines + DWARF locals when a frame is opened.
  useEffect(() => {
    if (tier !== "loc") return;
    const fid = frame.id;
    if (locData[fid]) return;
    setLocData((d) => ({ ...d, [fid]: { status: "loading" } }));
    fetch(`${SOURCE.apiBase}/scenario/${SOURCE.scenarioId}/frame/${fid}/loc`,
          { headers: { Accept: "application/json" } })
      .then((r) => r.text().then((txt) => {
        let j = null; try { j = txt ? JSON.parse(txt) : null; } catch (e) {}
        if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
        return j;
      }))
      .then((j) => setLocData((d) => ({ ...d, [fid]: { status: "ok", data: window.CairnAdapter.adaptLoc(j) } })))
      .catch((err) => setLocData((d) => ({ ...d, [fid]: { status: "unavailable", error: String(err.message || err) } })));
  }, [tier, frame.id, locData]);

  // apply tweaks to the document
  useEffect(() => { document.body.setAttribute("data-theme", t.theme); }, [t.theme]);
  useEffect(() => {
    document.documentElement.style.setProperty("--accent", t.accent);
    document.documentElement.style.setProperty("--accent-soft", t.accent + "33");
  }, [t.accent]);
  useEffect(() => {
    document.documentElement.style.setProperty("--anim", String(1 / t.animSpeed));
  }, [t.animSpeed]);

  const goTier = useCallback((next) => {
    setDir(TIER_RANK[next] > TIER_RANK[tier] ? "in" : "out");
    setTier(next);
    setPaneKey((k) => k + 1);
  }, [tier]);

  const visitFrame = useCallback((idx) => {
    const f = TRACE.frames[idx];
    if (!visited.current.has(f.id)) {
      visited.current.add(f.id);
      setModel((m) => absorbModel(m, f));
    }
  }, []);

  const zoomToFrame = useCallback((idx) => {
    setFrameIdx(idx);
    visitFrame(idx);
    goTier("loc");
  }, [goTier, visitFrame]);

  const jumpToFrameId = useCallback((fid) => {
    const idx = TRACE.frames.findIndex((f) => f.id === fid);
    if (idx >= 0) zoomToFrame(idx);
  }, [zoomToFrame]);

  // is the current frame arming a prediction invite?
  const armed = useMemo(() => {
    if (narrateOnly) return false;
    if (!frame.branch || !frame.branch.surprising) return false;
    if (predictLog[frame.id]) return false;
    if (skipPredict.has(frame.id)) return false;
    if (model.branch.status === "quiet") return false; // understood → stop asking
    return true;
  }, [frame, narrateOnly, predictLog, skipPredict, model.branch.status]);

  const predict = useMemo(() => {
    if (!frame.branch || !frame.branch.surprising) return null;
    const logged = predictLog[frame.id];
    if (logged) return { frame: frame.id, answered: true, choice: logged.choice };
    if (armed) return { frame: frame.id, answered: false, choice: null };
    return null;
  }, [frame, predictLog, armed]);

  const commitPredict = useCallback((choiceId) => {
    const b = frame.branch;
    const correct = choiceId === b.correct;
    setPredictLog((pl) => ({ ...pl, [frame.id]: { choice: choiceId, correct, declined: false } }));
    setModel((m) => {
      const e = { ...m.branch };
      if (correct) { e.confidence = Math.min(1, e.confidence + 0.65); e.status = "quiet"; }
      else { e.confidence = Math.max(0.12, e.confidence * 0.4); e.status = "asking"; }
      return { ...m, branch: e };
    });
  }, [frame]);

  const advance = useCallback(() => {
    // Enter blows straight past an unanswered invite (declined, no penalty)
    if (armed) {
      setPredictLog((pl) => ({ ...pl, [frame.id]: { choice: null, correct: false, declined: true } }));
    }
    if (frameIdx < TRACE.frames.length - 1) {
      const ni = frameIdx + 1;
      setFrameIdx(ni);
      visitFrame(ni);
      // stay in the current tier — arrow/d/f stepping through the skeleton moves
      // the playhead along the swimlane; it shouldn't drop you into LOC.
    }
  }, [armed, frame, frameIdx, visitFrame]);

  // Enter (and the ↵ button) advances AND dives to the code — the "step into it"
  // gesture, distinct from plain stepping which preserves the tier.
  const advanceDive = useCallback(() => {
    advance();
    if (tier !== "loc") goTier("loc");
  }, [advance, tier, goTier]);

  const stepBack = useCallback(() => {
    if (frameIdx > 0) { setFrameIdx(frameIdx - 1); }
  }, [frameIdx]);

  const skipHere = useCallback(() => {
    if (armed) setSkipPredict((s) => new Set(s).add(frame.id));
  }, [armed, frame]);

  const toggleModule = useCallback((mod) => {
    setCollapsed((c) => { const n = new Set(c); n.has(mod) ? n.delete(mod) : n.add(mod); return n; });
  }, []);

  // keyboard
  useEffect(() => {
    const onKey = (e) => {
      const tag = (e.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      const k = e.key.toLowerCase();
      // On the summary, ←/→ move a selection cursor across the grounded
      // highlights and Enter dives into the selected one (frame-stepping has no
      // meaning here — the page is the whole story, not one frame).
      if (tier === "precis" && (e.key === "ArrowRight" || k === "f" || e.key === "ArrowLeft" || k === "d" || e.key === "Enter")) {
        e.preventDefault();
        const links = window.precisLinks(TRACE);
        if (!links.length) return;
        if (e.key === "Enter") { jumpToFrameId(links[Math.min(precisSel, links.length - 1)].frame); }
        else if (e.key === "ArrowRight" || k === "f") { setPrecisSel((s) => Math.min(links.length - 1, s + 1)); }
        else { setPrecisSel((s) => Math.max(0, s - 1)); }
        return;
      }
      if (e.key === "Enter") { e.preventDefault(); advanceDive(); }
      else if (e.key === "Escape") { e.preventDefault(); skipHere(); }
      // step: ← / d  back · → / f  forward
      else if (e.key === "ArrowRight" || k === "f") { e.preventDefault(); advance(); }
      else if (e.key === "ArrowLeft" || k === "d") { e.preventDefault(); stepBack(); }
      // zoom: ↑ / k  out (toward summary) · ↓ / j  in (toward code)
      else if (e.key === "ArrowUp" || e.key === "[" || k === "k") { e.preventDefault();
        goTier(tier === "loc" ? "skeleton" : "precis"); }
      else if (e.key === "ArrowDown" || e.key === "]" || k === "j") { e.preventDefault();
        goTier(tier === "precis" ? "skeleton" : "loc"); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [advance, advanceDive, skipHere, stepBack, goTier, tier, precisSel, jumpToFrameId]);

  const atEnd = frameIdx >= TRACE.frames.length - 1;

  return (
    <div className="app">
      <aside className="spine">
        <div className="spine-head">
          <div className="brand">
            <CairnMark />
            <span className="brand-name">Cairn</span>
          </div>
          <div className="scenario">
            <div className="scenario-title">{TRACE.scenario.title}</div>
            <div className="scenario-sub">{TRACE.scenario.subtitle}</div>
            <div className="scenario-sub" style={{ marginTop: 6, color: "var(--text-dim)" }}>
              {TRACE.scenario.crate} · {TRACE.scenario.capture_backend}
            </div>
          </div>
        </div>
        <ZoomLadder tier={tier} onTier={goTier} frame={frame} />
        <UnderstandingMap model={model} />
      </aside>

      <main className="stage">
        <div className="topbar">
          <div className="seg">
            <button className={!narrateOnly ? "on predictpill" : ""} onClick={() => setNarrateOnly(false)}>
              ⌥ predict
            </button>
            <button className={narrateOnly ? "on" : ""} onClick={() => setNarrateOnly(true)}>
              ▸ just narrate
            </button>
          </div>
          <div className="topbar-spacer" />
          <button className="config-btn" onClick={() => setShowConfig(true)} title="configure & record a run">
            ⚙ record
          </button>
          <span className="source-badge live" title={"live trace · " + SOURCE.scenarioId}>● live</span>
          <div className="frame-counter">
            frame <span className="val">{frameIdx}</span> / {TRACE.frames.length - 1}
          </div>
        </div>

        <div className="stage-body">
          <div className={"pane " + (dir === "in" ? "enter-in" : "enter-out")} key={paneKey}>
            {tier === "precis" && (
              <PrecisView trace={TRACE} onJumpFrame={jumpToFrameId} activeFrameId={frame.id} selIndex={precisSel} />
            )}
            {tier === "skeleton" && (
              <SkeletonView trace={TRACE} frameIdx={frameIdx} collapsed={collapsed}
                            onToggle={toggleModule} onSetCollapsed={setCollapsed}
                            onZoomFrame={zoomToFrame} accent={t.accent} />
            )}
            {tier === "loc" && (
              <LocView frame={frame} frameIdx={frameIdx} predict={predict} onPredict={commitPredict}
                       loc={locData[frame.id]} apiBase={SOURCE.apiBase} scenarioId={SOURCE.scenarioId} />
            )}
          </div>
        </div>

        <div className="transport">
          <div className="stepper">
            <button className="stepbtn" onClick={stepBack} disabled={frameIdx === 0} title="step back (←)">‹</button>
            <button className="stepbtn" onClick={advance} disabled={atEnd && !armed} title="step forward (→)">›</button>
          </div>
          <button className="advance" onClick={advanceDive} disabled={atEnd && !armed}>
            {armed ? "blow past" : atEnd ? "end of run" : "advance"} <span style={{ opacity: .7 }}>↵</span>
          </button>

          <div className="transport-hint">
            {tier === "loc" && armed && (
              <span><span className="kbd">click</span> an arm to predict · <span className="kbd">Enter</span> past · <span className="kbd">Esc</span> skip</span>
            )}
            {tier === "loc" && !armed && (
              <span><span className="kbd">↑</span> zoom out to skeleton · <span className="kbd">←</span><span className="kbd">→</span> step</span>
            )}
            {tier === "skeleton" && (
              <span><span className="kbd">click a node</span> to zoom into its code · <span className="kbd">↑</span> summary · <span className="kbd">↓</span> code</span>
            )}
            {tier === "precis" && (
              <span><span className="kbd">click a clause</span> to drop to the code that proves it · <span className="kbd">↓</span> skeleton</span>
            )}
          </div>
        </div>
      </main>

      {showConfig && <RecordPanel apiBase={SOURCE.apiBase} onClose={() => setShowConfig(false)} />}

      <TweaksPanel>
        <TweakSection label="Theme" />
        <TweakRadio label="Surface" value={t.theme} options={["dark", "light", "hybrid"]}
                    onChange={(v) => setTweak("theme", v)} />
        <TweakColor label="Lit path" value={t.accent}
                    options={["#F2A93B", "#74D3BC", "#C29BF0", "#FF6B5E", "#5BA8F2"]}
                    onChange={(v) => setTweak("accent", v)} />
        <TweakSection label="Motion" />
        <TweakSlider label="Zoom speed" value={t.animSpeed} min={0.4} max={1.8} step={0.1} unit="×"
                     onChange={(v) => setTweak("animSpeed", v)} />
      </TweaksPanel>
    </div>
  );
}

// Shown when the live trace can't be loaded. No demo, no substitute data — the
// app states plainly that the backend is unavailable and how to retry.
function LoadError({ error, apiBase, scenarioId }) {
  return (
    <div className="load-error">
      <CairnMark size={28} />
      <div className="le-title serif">No live trace</div>
      <div className="le-msg">{error}</div>
      <div className="le-meta">
        <div><span className="le-k">scenario</span> <span className="le-v">{scenarioId}</span></div>
        <div><span className="le-k">api</span> <span className="le-v">{apiBase}</span></div>
      </div>
      <div className="le-hint">
        Start the backend (<code>cairn/api.py</code>), then <button className="le-retry" onClick={() => window.location.reload()}>retry</button>.
      </div>
    </div>
  );
}

// Bootstrap: fetch the live trace before the first render, so the synchronous
// App can keep reading the module-level TRACE. On failure, render the error.
const __root = ReactDOM.createRoot(document.getElementById("root"));
window.cairnLoadTrace().then((res) => {
  if (res.error || !res.trace) {
    console.warn("[cairn] live load failed:", res.error);
    __root.render(<LoadError error={res.error || "no trace"} apiBase={res.apiBase} scenarioId={res.scenarioId} />);
    return;
  }
  TRACE = res.trace;
  SOURCE = { apiBase: res.apiBase, scenarioId: res.scenarioId };
  __root.render(<App />);
});
