/* Tier 1 — system précis. Plain-language story of what this run did to the
   world. Every clause is backed by a real frame; click it to zoom to its
   grounding. The cheapest-density understanding-transfer: what, before how. */

// Frame keyed by leaf function name (first occurrence wins).
function precisByName(trace) {
  const m = {};
  trace.frames.forEach((f) => {
    const s = f.lane.split("::").pop();
    if (s && !(s in m)) m[s] = f;
  });
  return m;
}

// Ordered list of the highlighted (grounded) function-name links in the prose —
// shared by the view (rendering/selection) and the app (←/→ navigation). Each
// link is a real frame the named function executed in.
function precisLinks(trace) {
  const p = typeof trace.precis === "string" ? trace.precis : String(trace.precis || "");
  const byName = precisByName(trace);
  const parts = p.split(/([A-Za-z_][A-Za-z0-9_]*)/);
  const links = [];
  parts.forEach((tok, i) => {
    if (i % 2 === 1 && byName[tok]) links.push({ token: tok, frame: byName[tok].id });
  });
  return links;
}

function PrecisView({ trace, onJumpFrame, activeFrameId, selIndex }) {
  // The backend précis is a prose string — rendered verbatim (never reworded).
  // We only make the function names it mentions clickable: each links to the
  // frame for that function, so the link is grounded, not invented. ←/→ moves a
  // selection cursor across these highlights; Enter dives into the selected one.
  const p = typeof trace.precis === "string" ? trace.precis : String(trace.precis || "");
  const byName = React.useMemo(() => precisByName(trace), [trace]);
  const selRef = React.useRef(null);

  React.useEffect(() => {
    if (selRef.current) selRef.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selIndex]);

  // split on identifier runs (capture group keeps them); odd indices are tokens.
  const parts = p.split(/([A-Za-z_][A-Za-z0-9_]*)/);
  let linkIdx = -1;
  const body = parts.map((tok, i) => {
    const f = byName[tok];
    if (i % 2 === 1 && f) {
      linkIdx++;
      const isSel = linkIdx === selIndex;
      return (
        <span key={i} ref={isSel ? selRef : null}
              className={"clause" + (f.branch && f.branch.surprising ? " surprising" : "") +
                         (f.id === activeFrameId ? " active" : "") + (isSel ? " sel" : "")}
              onClick={() => onJumpFrame(f.id)}
              title={"zoom to " + tok + "()"}>
          {tok}
        </span>
      );
    }
    return <React.Fragment key={i}>{tok}</React.Fragment>;
  });

  const linked = linkIdx + 1;
  return (
    <div className="precis-wrap">
      <div className="precis-inner">
        <div className="eyebrow">Tier 1 · summary</div>
        <h1 className="precis-title serif">Summary</h1>
        <p className="precis-body serif">{body}</p>
        <div className="precis-foot">
          <span className="pf-dot" />
          {linked > 0
            ? <>The backend's prose, rendered verbatim. The <span className="surprising-ink">highlighted</span> function names are grounded links — <span className="kbd">←</span><span className="kbd">→</span> to move across them, <span className="kbd">Enter</span> to drop into the selected frame.</>
            : "The backend's prose, rendered verbatim — a projection of the one recording, never rewritten here."}
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { PrecisView, precisLinks });
