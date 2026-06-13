/* Tier 2 — the causal skeleton (swimlane).
   Y = functions in stable source order (containment via module groups, collapsible).
   X = logical step (idle collapses; hot loops are one ×N band).
   The executed path is a lit, directed, NUMBERED line that hops between lanes.
   Road-not-taken arms are dim dashed stubs. The I/O excursion is a visible seam.
   A long vertical jump is rendered fully — it is signal, not a bug. */

const ROW_H = 56;
const STEP_W = 132;
const IO_GAP = 92;
const LABEL_W = 196;

function buildLayout(trace, collapsed) {
  // group consecutive lanes by module
  const groups = [];
  trace.lanes.forEach((lane) => {
    const g = groups[groups.length - 1];
    if (g && g.module === lane.module) g.lanes.push(lane);
    else groups.push({ module: lane.module, lanes: [lane] });
  });
  const collapsibleModules = groups
    .filter((g) => g.lanes.length > 1 && !g.module.startsWith("::"))
    .map((g) => g.module);

  // visual rows + laneId -> rowIndex
  const rows = [];
  const rowOf = {};
  groups.forEach((g) => {
    const collapsible = g.lanes.length > 1 && !g.module.startsWith("::");
    if (collapsible && collapsed.has(g.module)) {
      const idx = rows.length;
      rows.push({ type: "module", module: g.module, lanes: g.lanes, collapsible: true });
      g.lanes.forEach((l) => (rowOf[l.id] = idx));
    } else {
      g.lanes.forEach((l, i) => {
        const idx = rows.length;
        rows.push({ type: "lane", lane: l, module: g.module, collapsible, first: i === 0, count: g.lanes.length });
        rowOf[l.id] = idx;
      });
    }
  });

  // x positions per step, inserting a seam before any I/O step
  const ioSteps = new Set(trace.boundaries.map((b) => b.step));
  const maxStep = Math.max(...trace.frames.map((f) => f.step));
  const stepX = [];
  let cursor = STEP_W * 0.5;
  const seamX = {};
  for (let s = 0; s <= maxStep; s++) {
    if (ioSteps.has(s)) { seamX[s] = cursor + IO_GAP * 0.5; cursor += IO_GAP; }
    stepX[s] = cursor;
    cursor += STEP_W;
  }
  const plotW = cursor;
  const plotH = rows.length * ROW_H;

  const nodes = trace.frames.map((f, i) => ({
    f, i,
    x: stepX[f.step],
    y: rowOf[f.lane] * ROW_H + ROW_H / 2,
    row: rowOf[f.lane],
  }));

  return { rows, rowOf, nodes, stepX, seamX, plotW, plotH, maxStep, collapsibleModules };
}

function SkeletonView({ trace, frameIdx, collapsed, onToggle, onSetCollapsed, onZoomFrame, accent }) {
  const L = React.useMemo(() => buildLayout(trace, collapsed), [trace, collapsed]);
  const scrollRef = React.useRef(null);
  const labelsRef = React.useRef(null);

  // lane labels are pinned horizontally but follow the plot vertically.
  const syncLabels = React.useCallback(() => {
    const el = scrollRef.current;
    if (el && labelsRef.current) labelsRef.current.style.transform = "translateY(" + (-el.scrollTop) + "px)";
  }, []);

  // keep current node in view (both axes — the swimlane can be taller than the pane)
  React.useEffect(() => {
    const n = L.nodes[frameIdx];
    const el = scrollRef.current;
    if (n && el) {
      el.scrollTo({
        left: Math.max(0, n.x - el.clientWidth * 0.5),
        top: Math.max(0, n.y - el.clientHeight * 0.5),
        behavior: "smooth",
      });
    }
    // re-sync the lane labels immediately — collapse/expand reflows the rows but
    // may not fire a scroll event, which would leave the label transform stale.
    syncLabels();
  }, [frameIdx, L, syncLabels]);

  const cur = L.nodes[frameIdx];

  // path string through nodes
  const segs = [];
  for (let k = 1; k < L.nodes.length; k++) {
    const a = L.nodes[k - 1], b = L.nodes[k];
    segs.push({ a, b, k, traveled: k <= frameIdx, jump: Math.abs(a.row - b.row) >= 4 });
  }

  const provRow = L.rowOf["provider"];

  return (
    <div className="skel-wrap terrain">
      <div className="skel-head">
        <div>
          <div className="eyebrow">Tier 2 · causal skeleton</div>
          <div className="skel-title serif">The path this run actually took</div>
          {L.collapsibleModules.length > 0 && (() => {
            const allCollapsed = L.collapsibleModules.every((m) => collapsed.has(m));
            return (
              <button className="skel-allbtn"
                      onClick={() => onSetCollapsed(allCollapsed ? new Set() : new Set(L.collapsibleModules))}
                      title={allCollapsed ? "expand every module" : "collapse every module"}>
                {allCollapsed ? "⊕ expand all" : "⊖ collapse all"}
              </button>
            );
          })()}
        </div>
        <div className="skel-legend">
          <span className="lg"><i className="lg-line" style={{ background: accent }} />executed</span>
          <span className="lg"><i className="lg-dash" />road not taken</span>
          <span className="lg"><i className="lg-seam" />I/O excursion</span>
          <span className="lg"><i className="lg-dorm" />never ran</span>
        </div>
      </div>

      <div className="skel-grid">
        {/* sticky lane labels — pinned in X, translated in Y to match the plot */}
        <div className="lane-labels" style={{ width: LABEL_W }}>
         <div className="lane-labels-inner" ref={labelsRef}>
          {L.rows.map((r, i) => {
            if (r.type === "module") {
              return (
                <div className="lane-label module collapsed" style={{ height: ROW_H }} key={i}
                     onClick={() => onToggle(r.module)} title="expand functions">
                  <span className="caret">▸</span>
                  <span className="ll-mod">{r.module}</span>
                  <span className="ll-count">{r.lanes.length} fns</span>
                </div>
              );
            }
            const l = r.lane;
            return (
              <div className={"lane-label" + (l.dormant ? " dormant" : "") + (l.external ? " external" : "")}
                   style={{ height: ROW_H }} key={i}
                   onClick={() => r.collapsible && onToggle(r.module)}
                   title={r.collapsible ? "collapse module" : l.label}>
                {r.first && r.collapsible && <span className="caret open">▾</span>}
                {!(r.first && r.collapsible) && <span className="caret-pad" />}
                <span className="ll-meta">
                  <span className="ll-mod-sm">{l.module}</span>
                  <span className="ll-fn">{l.short}</span>
                </span>
                {l.dormant && <span className="ll-dorm-tag">dormant</span>}
              </div>
            );
          })}
         </div>
        </div>

        {/* scrollable plot — scrolls both axes; drives the lane labels in Y */}
        <div className="plot-scroll" ref={scrollRef} onScroll={syncLabels}>
          <div className="plot" style={{ width: L.plotW, height: L.plotH }}>
            {/* lane stripes */}
            {L.rows.map((r, i) => (
              <div key={i} className={"lane-stripe" + (i % 2 ? " odd" : "") + ((r.lane && r.lane.dormant) ? " dormant" : "")}
                   style={{ top: i * ROW_H, height: ROW_H }} />
            ))}

            {/* I/O seams */}
            {trace.boundaries.map((b, i) => (
              <div className="io-seam" key={i} style={{ left: L.seamX[b.step] - IO_GAP / 2, width: IO_GAP, height: L.plotH }}>
                <span className="io-seam-label">↗ left process · {b.detail.split("·").pop().trim()}</span>
              </div>
            ))}

            {/* edges */}
            <svg className="plot-svg" width={L.plotW} height={L.plotH}>
              <defs>
                <marker id="arrow" markerWidth="9" markerHeight="9" refX="6.5" refY="4.5" orient="auto">
                  <path d="M1,1 L7,4.5 L1,8" fill="none" stroke={accent} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
                </marker>
                <marker id="arrowDim" markerWidth="9" markerHeight="9" refX="6.5" refY="4.5" orient="auto">
                  <path d="M1,1 L7,4.5 L1,8" fill="none" stroke={accent} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" opacity="0.4" />
                </marker>
              </defs>

              {/* excursion to provider on the I/O frame */}
              {trace.frames.filter((f) => f.kind === "io").map((f) => {
                const n = L.nodes[f.i ?? trace.frames.indexOf(f)];
                const node = L.nodes.find((nn) => nn.f.id === f.id);
                if (provRow == null || !node) return null;
                const py = provRow * ROW_H + ROW_H / 2;
                return (
                  <path key={f.id} d={`M${node.x},${node.y} L${node.x},${py}`}
                        stroke={accent} strokeWidth="1.5" strokeDasharray="4 4" fill="none" opacity="0.55" />
                );
              })}

              {/* road-not-taken dashed stubs at branches */}
              {L.nodes.map((n) => {
                if (n.f.kind !== "branch") return null;
                const dir = n.row >= L.rows.length - 2 ? -1 : 1;
                const ex = n.x + 30, ey = n.y + dir * 22;
                return (
                  <g key={"nt" + n.f.id}>
                    <path d={`M${n.x},${n.y} q 18,${dir * 6} 30,${dir * 22}`}
                          stroke="var(--not-taken)" strokeWidth="1.5" strokeDasharray="3 4" fill="none" />
                    <circle cx={ex} cy={ey} r="3" fill="none" stroke="var(--not-taken)" strokeWidth="1.3" />
                  </g>
                );
              })}

              {/* executed path segments */}
              {segs.map((s) => {
                const dim = !s.traveled;
                return (
                  <path key={s.k}
                        d={`M${s.a.x},${s.a.y} L${s.b.x},${s.b.y}`}
                        stroke={accent} strokeWidth={s.traveled ? 2.6 : 1.8}
                        opacity={dim ? 0.32 : 1}
                        strokeLinecap="round"
                        markerEnd={dim ? "url(#arrowDim)" : "url(#arrow)"}
                        className={s.traveled ? "seg-lit" : "seg-ahead"} />
                );
              })}
            </svg>

            {/* nodes */}
            {L.nodes.map((n) => {
              const isCur = n.i === frameIdx;
              const done = n.i <= frameIdx;
              const f = n.f;
              return (
                <button key={f.id}
                        className={"snode k-" + f.kind + (isCur ? " current" : "") + (done ? " done" : " ahead") + (f.branch && f.branch.surprising ? " surprising" : "")}
                        style={{ left: n.x, top: n.y }}
                        title={f.role}
                        onClick={() => onZoomFrame(n.i)}>
                  <span className="snode-num">{n.i}</span>
                  {f.kind === "loop_collapsed" && <span className="snode-loop">×{f.loop_count}</span>}
                  {f.branch && f.branch.surprising && <span className="snode-spark">⌥</span>}
                  <span className="snode-label">{f.code.fn}</span>
                </button>
              );
            })}

            {/* playhead ring */}
            {cur && <div className="playhead" style={{ left: cur.x, top: cur.y }} />}
          </div>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { SkeletonView, buildLayout });
