/* Left spine — holds still across all zoom tiers.
   Brand, the zoom ladder (your place: Précis ▸ Skeleton ▸ Frame),
   and the understanding map (frontier model made visible). */

const KIND_META = {
  call:           { label: "Calls",          glyph: "→" },
  branch:         { label: "Branches",        glyph: "⌥" },
  io:             { label: "I/O boundaries",  glyph: "⇄" },
  loop_collapsed: { label: "Loops",           glyph: "↻" },
  return:         { label: "Returns",         glyph: "←" },
};

function CairnMark({ size = 22 }) {
  // stacked trail-marker stones
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 24 24" fill="none">
      <ellipse cx="12" cy="20" rx="8.5" ry="2.2" fill="var(--accent)" opacity="0.9" />
      <ellipse cx="12" cy="14.5" rx="6.2" ry="3" fill="var(--text-mid)" />
      <ellipse cx="12.4" cy="9.4" rx="4.3" ry="2.4" fill="var(--accent)" />
      <circle cx="12" cy="5.2" r="2.6" fill="var(--text-hi)" />
    </svg>
  );
}

function ZoomLadder({ tier, onTier, frame }) {
  const rungs = [
    { id: "precis",   tierLabel: "Tier 1", name: "Summary",  sub: "the whole story" },
    { id: "skeleton", tierLabel: "Tier 2", name: "Skeleton", sub: "causal path" },
    { id: "loc",      tierLabel: "Tier 3", name: frame ? frame.code.fn + "()" : "Frame", sub: frame ? frame.file.split("/").pop() : "code + state" },
  ];
  return (
    <div className="ladder">
      <div className="eyebrow" style={{ marginBottom: 10 }}>You are here</div>
      {rungs.map((r, i) => (
        <React.Fragment key={r.id}>
          <div className={"ladder-rung" + (tier === r.id ? " active" : "")} onClick={() => onTier(r.id)}>
            <span className="rung-dot" />
            <span className="rung-meta">
              <span className="rung-tier">{r.tierLabel}</span>
              <span className="rung-name">{r.name}</span>
            </span>
          </div>
          {i < rungs.length - 1 && <div className="ladder-line" />}
        </React.Fragment>
      ))}
    </div>
  );
}

function UnderstandingMap({ model }) {
  const kinds = Object.keys(KIND_META).filter((k) => model[k]);
  const overall = kinds.length
    ? Math.round(kinds.reduce((s, k) => s + model[k].confidence, 0) / kinds.length * 100)
    : 0;
  const statusFor = (m) => {
    if (m.status === "quiet") return ["quiet", "charted"];
    if (m.status === "asking") return ["asking", "expanding"];
    return ["cold", "unseen"];
  };
  return (
    <div className="umap terrain-typeproof">
      <div className="umap-head">
        <span className="umap-title">Map of understanding</span>
        <span className="umap-pct">{overall}% charted</span>
      </div>
      {kinds.map((k) => {
        const m = model[k];
        const [statusClass, statusText] = statusFor(m);
        return (
          <div className="umap-row" key={k}>
            <div className="umap-row-head">
              <span style={{ color: "var(--text-dim)", width: 14, textAlign: "center" }}>{KIND_META[k].glyph}</span>
              <span className="umap-kind">{KIND_META[k].label}</span>
              <span className={"umap-status " + statusClass}>{statusText}</span>
            </div>
            <div className="umap-bar">
              <div className="umap-fill" style={{
                width: Math.round(m.confidence * 100) + "%",
                background: m.status === "asking" ? "var(--accent)" : "var(--charted)",
              }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

Object.assign(window, { KIND_META, CairnMark, ZoomLadder, UnderstandingMap });
