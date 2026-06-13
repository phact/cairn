/* Tier 3 — LOC + state. The deepest zoom: real code, executed lines lit,
   recorded values inline, road-not-taken drawn dim & dashed in place.
   Also the host surface for a predict invitation at a surprising fork. */

const EVENT_COLOR = { read: "var(--value)", born: "var(--charted)", mutated: "var(--predict)" };
const EVENT_TAG = { read: "read", born: "born", mutated: "mutated" };

// A value that lives behind a pointer/reference carries a structured `ref`
// { kind: "ptr"|"ref", address }. We badge the address (demoted, full on hover)
// so the clean contents stay front-and-center. Tolerant of ref being absent.
function shortAddr(addr) {
  const s = String(addr || "");
  return s.length > 8 ? "0x…" + s.slice(-4) : s;
}
function RefBadge({ refv }) {
  if (!refv || !refv.address) return null;
  return (
    <span className="ref-badge" title={(refv.kind || "ptr") + " " + refv.address}>
      {refv.kind || "ptr"} {shortAddr(refv.address)}
    </span>
  );
}

// Structured value tree from the backend ({ name, type, summary, children }).
// Rendered as a collapsible tree: each node shows its field name + a preview
// (its summary, or its short type), and expands to its children. Goes as deep
// as the backend sends — no string truncation.
function shortType(t) {
  return String(t == null ? "" : t).replace(/(?:[a-z_][A-Za-z0-9_]*::)+([A-Za-z_][A-Za-z0-9_]*)/g, "$1");
}
// Resolve a node by its `path` (["self","config","value"]) in a deref response.
function derefFind(loc, path) {
  for (const ln of loc.lines || []) {
    for (const v of ln.locals || []) {
      if (v.name === path[0] && v.tree) {
        let n = v.tree;
        for (const seg of path.slice(1)) {
          n = (n.children || []).find((c) => String(c.name) === String(seg));
          if (!n) break;
        }
        if (n) return n;
      }
    }
  }
  return null;
}

// A node may carry a decoded JWT ({header:{alg,kid,typ}, claims:{...}}) — render
// the real identity instead of leaving it as an opaque base64 blob.
function JwtBlock({ jwt }) {
  const h = jwt.header || {};
  const claims = jwt.claims || {};
  return (
    <div className="vt-jwt">
      <div className="vt-jwt-h">
        decoded JWT
        {h.alg && <span className="vt-jwt-alg">{h.alg}{h.kid ? " · " + h.kid : ""}</span>}
      </div>
      {Object.keys(claims).map((k) => (
        <div className="vt-jwt-row" key={k}>
          <span className="vt-jwt-k">{k}</span>
          <span className="vt-jwt-v">{String(claims[k])}</span>
        </div>
      ))}
    </div>
  );
}

function ValueTree({ node, depth, openTo, ctx }) {
  // Key strictly on deref.status — NOT on shape/children (spec: an empty pointer
  // and a budget-stopped pointer look identical by shape). The dereference
  // affordance shows iff status === "unexpanded"; that's the only state where
  // ground truth exists but wasn't followed. null/unreadable/expanded → no button.
  const dstatus = node.deref && node.deref.status;
  const eagerKids = node.children && node.children.length ? node.children : null;
  const derefPath = node.deref && node.deref.path;
  const unexpanded = dstatus === "unexpanded" && derefPath && ctx;

  const [lazyKids, setLazyKids] = React.useState(null);   // null=unfetched, []=fetched-empty
  const [rstate, setRstate] = React.useState("idle");     // idle | loading | error
  const [rerr, setRerr] = React.useState(null);
  const kids = eagerKids || lazyKids;
  const [open, setOpen] = React.useState(!!eagerKids && depth < openTo);

  // Resolve an unexpanded pointer via /frame/:fid/value (path = JSON array,
  // time-indexed). Follows the pointer as it was at the frame's instant.
  const resolve = () => {
    if (rstate === "loading") return;
    setRstate("loading"); setRerr(null);
    const pj = encodeURIComponent(JSON.stringify(derefPath));
    const inst = ctx.instant != null ? "&instant=" + ctx.instant : "";
    fetch(`${ctx.apiBase}/scenario/${ctx.scenarioId}/frame/${ctx.fid}/value?path=${pj}&depth=2${inst}`,
          { headers: { Accept: "application/json" } })
      .then((r) => r.text().then((t) => {
        let j = null; try { j = t ? JSON.parse(t) : null; } catch (e) {}
        if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
        return j;
      }))
      .then((res) => {
        // response is either a full loc (find node by path) or the node subtree itself
        const found = res && res.lines ? derefFind(res, derefPath) : res;
        setLazyKids((found && found.children) || []);
        setRstate("idle"); setOpen(true);
      })
      .catch((e) => { setRerr(String(e.message || e)); setRstate("error"); });
  };

  const onClick = () => {
    if (unexpanded && lazyKids === null) { resolve(); return; }   // first click → follow pointer
    if (kids) setOpen((o) => !o);
  };

  const interactive = !!kids || unexpanded;
  const caret = rstate === "loading" ? "⋯" : interactive ? (open ? "▾" : "▸") : "";
  // collapse module qualifiers in both summary and type previews
  const preview = node.summary ? shortType(node.summary) : (node.type ? shortType(node.type) : "");

  return (
    <div className="vt-node">
      <div className={"vt-head" + (interactive ? " has-kids" : "") + (unexpanded ? " lazy" : "")}
           onClick={interactive ? onClick : undefined}>
        <span className="vt-caret">{caret}</span>
        {node.name && <span className="vt-name">{node.name}</span>}
        {node.name && preview ? <span className="vt-sep">:</span> : null}
        {preview && <span className="vt-preview">{preview}</span>}
        {node.as_time && <span className="vt-time" title="decoded timestamp">⏱ {node.as_time}</span>}
        {/* affordance ONLY for unexpanded; distinct, non-clickable markers otherwise */}
        {unexpanded && lazyKids === null && rstate === "idle" && <span className="vt-deref" title={node.deref.reason || "not followed"}>deref →</span>}
        {dstatus === "unreadable" && <span className="vt-unreadable" title="memory not readable at this instant (freed/optimized out)">unreadable</span>}
      </div>
      {node.jwt && <div className="vt-kids"><JwtBlock jwt={node.jwt} /></div>}
      {rstate === "error" && <div className="vt-err" onClick={resolve} title="retry">couldn’t follow pointer — {rerr}</div>}
      {kids && open && (
        kids.length
          ? <div className="vt-kids">
              {kids.map((c, i) => <ValueTree key={i} node={c} depth={depth + 1} openTo={openTo} ctx={ctx} />)}
            </div>
          : <div className="vt-empty">followed — no fields at this instant</div>
      )}
    </div>
  );
}

// Fallback for values that arrive as a flat debug string (no structured tree):
// reflow into an indented block. Cosmetic; tolerant of a trailing "…".
function structIsTree(s) { return s && /[{([]/.test(s) && s.length > 40; }
function formatStruct(s) {
  let out = "", depth = 0, inStr = false;
  const pad = (d) => "  ".repeat(Math.max(0, d));
  const skipSpaces = (i) => { while (i + 1 < s.length && s[i + 1] === " ") i++; return i; };
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (inStr) { out += c; if (c === '"' && s[i - 1] !== "\\") inStr = false; continue; }
    if (c === '"') { inStr = true; out += c; continue; }
    if (c === "{" || c === "(" || c === "[") { depth++; out += c + "\n" + pad(depth); i = skipSpaces(i); }
    else if (c === "}" || c === ")" || c === "]") { depth = Math.max(0, depth - 1); out += "\n" + pad(depth) + c; }
    else if (c === ",") { out += ",\n" + pad(depth); i = skipSpaces(i); }
    else out += c;
  }
  return out;
}

function valuesForLine(line, values) {
  // whole-word matcher: a value annotates a line if its leaf name appears as a
  // token (so "aud" doesn't match inside "verify_aud"). Cap at 2 per line.
  const out = [];
  for (const v of values) {
    const leaf = v.name.split(/[.\s]/).pop();
    if (!leaf || leaf.length < 2) continue;
    const re = new RegExp("\\b" + leaf.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b");
    if (re.test(line.t)) out.push(v);
    if (out.length >= 1) break;
  }
  return out;
}

function CodeLine({ line, frame, predict }) {
  const isBranch = frame.kind === "branch";
  const armTaken = line.arm === "taken";
  const armNot = line.arm === "not_taken";
  const revealing = predict && predict.frame === frame.id && predict.answered;
  const asking = predict && predict.frame === frame.id && !predict.answered;

  // during an unanswered prompt we don't pre-light the taken arm
  const showTaken = armTaken && (!asking);
  const inlineVals = valuesForLine(line, frame.values);

  let cls = "code-line";
  if (line.hot && !armNot) cls += " hot";
  if (showTaken) cls += " arm-taken";
  if (armNot) cls += " arm-not";
  if (asking && (armTaken || armNot)) cls += " candidate";

  return (
    <div className={cls}>
      <span className="ln">{line.n}</span>
      <span className="lt">{line.t}</span>
      {inlineVals.length > 0 && (
        <span className="line-vals">
          {inlineVals.map((v, i) => (
            <span className="inline-val" key={i} style={{ "--vc": EVENT_COLOR[v.event] }}>
              {v.name}<span className="iv-eq">=</span><span className="iv-v">{v.value}</span>
            </span>
          ))}
        </span>
      )}
      {armNot && <span className="arm-tag">road not taken</span>}
    </div>
  );
}

// Tier-3 replay (live). When the backend serves source `text` per line, show
// the real code with DWARF locals read at each line; otherwise fall back to
// executed line numbers + locals. Loading / unavailable states surfaced honestly.
function localsForLine(line) {
  // Show only the locals this line actually references, so the inline chips are
  // relevant to the line, not the whole scope repeated on every .await/.lock().
  // The full set of in-scope values is always in the Recorded State rail.
  // (When there is no source text — older backend — fall back to first few.)
  if (!line.text) return line.locals.slice(0, 3);
  return line.locals.filter((v) => {
    const leaf = v.name.split(/[.\s]/).pop();
    return leaf && leaf.length >= 2 && new RegExp("\\b" + leaf.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b").test(line.text);
  }).slice(0, 3);
}

function ReplayedLines({ loc }) {
  if (!loc || loc.status === "loading") {
    return <div className="replay-note">replaying this activation…</div>;
  }
  if (loc.status === "unavailable") {
    return (
      <div className="replay-note unavailable">
        line-level replay unavailable — {loc.error}
        <div className="replay-sub">recorded state for this frame is shown at right.</div>
      </div>
    );
  }
  const d = loc.data;
  if (!d || !d.lines.length) {
    return <div className="replay-note">no line events recorded for this activation.</div>;
  }
  return (
    <>
      {d.lines.map((line, i) => {
        const vals = localsForLine(line);
        return (
          <div className={"code-line replayed" + (vals.length ? " has-vals" : "")} key={i}>
            <span className="ln">{line.n}</span>
            {line.text != null
              ? <span className="lt">{line.text}</span>
              : <span className="lt dim">— executed —</span>}
            {vals.length > 0 && (
              <span className="line-vals">
                {vals.map((v, j) => (
                  <span className="inline-val" key={j} style={{ "--vc": "var(--value)" }} title={v.full || v.value}>
                    {v.name}<span className="iv-eq">=</span><span className="iv-v">{v.value}</span>
                    <RefBadge refv={v.ref} />
                  </span>
                ))}
              </span>
            )}
          </div>
        );
      })}
      {d.reverse && (
        <div className="reverse-note" title={d.reverse.matches ? "reverse re-read agreed with forward" : "reverse re-read"}>
          ⟲ reverse-step re-read line {d.reverse.line}
          {d.reverse.matches ? " · matches forward" : ""}
        </div>
      )}
    </>
  );
}

// The whole source file, with the executed lines lit and the current frame's
// line centered. Source comes from GET /scenario/:id/source?path= — real file
// text from the recording, never reconstructed.
function FullFileView({ apiBase, scenarioId, path, activeLine, onClose }) {
  const [state, setState] = React.useState({ status: "loading" });
  const activeRef = React.useRef(null);

  React.useEffect(() => {
    let live = true;
    setState({ status: "loading" });
    fetch(`${apiBase}/scenario/${scenarioId}/source?path=${encodeURIComponent(path)}`,
          { headers: { Accept: "application/json" } })
      .then((r) => r.text().then((txt) => {
        let j = null; try { j = txt ? JSON.parse(txt) : null; } catch (e) {}
        if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
        return j;
      }))
      .then((d) => { if (live) setState({ status: "ok", data: d }); })
      .catch((e) => { if (live) setState({ status: "error", error: String(e.message || e) }); });
    return () => { live = false; };
  }, [apiBase, scenarioId, path]);

  React.useEffect(() => {
    if (state.status === "ok" && activeRef.current) {
      activeRef.current.scrollIntoView({ block: "center" });
    }
  }, [state.status, activeLine]);

  const executed = React.useMemo(
    () => new Set((state.data && state.data.executed_lines) || []), [state.data]);

  return (
    <div className="fullfile">
      <div className="ff-head">
        <span className="ff-path">{path}</span>
        {state.data && <span className="ff-meta">{state.data.line_count} lines · {executed.size} executed</span>}
        <button className="ff-close" onClick={onClose} title="back to the executed slice">✕ close</button>
      </div>
      <div className="ff-body">
        {state.status === "loading" && <div className="replay-note">loading {path}…</div>}
        {state.status === "error" && <div className="replay-note unavailable">couldn't load file — {state.error}</div>}
        {state.status === "ok" && state.data.lines.map((ln) => {
          const isActive = ln.n === activeLine;
          return (
            <div key={ln.n} ref={isActive ? activeRef : null}
                 className={"ff-line" + (executed.has(ln.n) ? " exec" : "") + (isActive ? " active" : "")}>
              <span className="ln">{ln.n}</span>
              <span className="lt">{ln.text}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── grounded handoff (spec §"The explain question") ─────────────────────────
// Both are assembled client-side from data already on screen. The rule:
// send what only Cairn has (recorded values); never source the agent has, nor
// prose only a model can produce.
function decidingStr(dv) {
  if (!dv) return "";
  if (typeof dv === "string") return dv;
  return dv.name + (dv.value ? " = " + dv.value : "");
}
function stateEntries(frame, locVals) {
  return (frame.values || []).map((v) => {
    const best = locVals[v.name];
    return { name: v.name, value: (best && best.full) || v.full || v.value, event: v.event };
  });
}

// explain = POINTER for an agent that has the Cairn CLI: recorded state +
// follow-up commands, NO source, NO prose.
function buildExplain(frame, n, trace, locVals) {
  const b = frame.branch;
  const o = {
    trace: trace, frame: n,
    location: frame.file + ":" + frame.line,
    kind: frame.kind, role: frame.role,
  };
  if (b) o.branch = {
    taken_arm: b.taken_arm,
    deciding_value: decidingStr(b.deciding_value),
    not_taken: b.not_taken ? {
      arm: b.not_taken.arm, target_line: b.not_taken.target_line,
      summary: b.not_taken.summary, epistemic: "static",
    } : undefined,
  };
  o.recorded_state = {};
  stateEntries(frame, locVals).forEach((e) => { o.recorded_state[e.name] = { value: e.value, event: e.event }; });
  o.follow_up = [
    "cairn state " + n + " --json        # full recorded state at this frame",
    "cairn loc " + n + " --json          # line-by-line execution with values",
    "cairn step " + n + " --back         # reverse-step to re-read earlier state",
    n > 0 ? "cairn frame " + (n - 1) + " --json        # the prior salient frame" : null,
    "cairn source " + frame.file + ":" + frame.line + " --context 8   # source (you likely already have this)",
  ].filter(Boolean);
  o.grounding = "Explain only from recorded values above. `not_taken` is the static alternative arm — describe it with 'would', not 'did'. If you need a value not present here, run a follow_up command; do not guess.";
  return JSON.stringify(o, null, 2);
}

// handoff = self-contained BUNDLE for an unprimed harness: includes a source
// slice (the one command that does), recorded state, branch, grounding.
function buildHandoff(frame, n, trace, locVals, sourceSlice) {
  let s = "# Cairn handoff · " + trace + " · frame " + n + "\n";
  s += "# " + frame.file + ":" + frame.line + " · " + frame.kind + " · " + frame.role + "\n\n";
  if (sourceSlice) s += "## Source (recorded execution)\n```rust\n" + sourceSlice + "\n```\n\n";
  s += "## Recorded state (ground truth — decoded from the recording)\n";
  stateEntries(frame, locVals).forEach((e) => { s += "- " + e.name + " = " + e.value + "  [" + e.event + "]\n"; });
  const b = frame.branch;
  if (b) {
    s += "\n## Branch\n- took: " + b.taken_arm + "\n- decided by: " + decidingStr(b.deciding_value) + "\n";
    if (b.not_taken) s += "- road not taken (static alternative): " + b.not_taken.arm + " — would " + b.not_taken.summary + "\n";
  }
  s += "\n## Grounding\nExplain ONLY from the recorded values above. The road-not-taken arm is the static alternative — describe it with 'would', not 'did'. If you need a value that isn't here, say so; do not guess.\n";
  return s;
}

function LocView({ frame, frameIdx, predict, onPredict, loc, apiBase, scenarioId }) {
  const [showFile, setShowFile] = React.useState(false);
  const [copied, setCopied] = React.useState(null);
  if (!frame) return null;
  const c = frame.code;
  const b = frame.branch;
  const sourceless = c.sourceless && (!c.lines || c.lines.length === 0);
  const canFull = !!(frame.fullFile && apiBase && scenarioId);

  // The line-level replay decodes locals more richly than the artifact's frame
  // values (which may carry just a pointer). Index the richest decode per name
  // so the state rail can show full contents, not a bare address.
  const locVals = React.useMemo(() => {
    const m = {};
    if (loc && loc.status === "ok" && loc.data) {
      loc.data.lines.forEach((ln) => (ln.locals || []).forEach((v) => {
        const cur = m[v.name];
        if (!cur || (v.full || "").length > (cur.full || "").length) m[v.name] = v;
      }));
    }
    return m;
  }, [loc]);

  const n = frameIdx != null ? frameIdx : 0;
  const flash = (msg) => { setCopied(msg); window.setTimeout(() => setCopied(null), 1800); };
  const copy = (text, msg) =>
    navigator.clipboard.writeText(text).then(() => flash(msg)).catch(() => flash("copy failed"));

  const doExplain = () => copy(buildExplain(frame, n, scenarioId, locVals), "explain copied");
  const doHandoff = () => {
    const finish = (slice) => copy(buildHandoff(frame, n, scenarioId, locVals, slice), "handoff copied");
    if (frame.fullFile && apiBase && scenarioId) {
      fetch(`${apiBase}/scenario/${scenarioId}/source?path=${encodeURIComponent(frame.fullFile)}`,
            { headers: { Accept: "application/json" } })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (!d || !d.lines) return finish(null);
          const slice = d.lines
            .filter((l) => l.n >= frame.line - 8 && l.n <= frame.line + 8)
            .map((l) => String(l.n).padStart(4) + "  " + l.text).join("\n");
          finish(slice);
        })
        .catch(() => finish(null));
    } else finish(null);
  };

  if (showFile && canFull) {
    return (
      <div className="loc-wrap terrain">
        <div className="loc-head">
          <div className="loc-head-row">
            <span className={"kind-badge k-" + frame.kind}>{frame.kind.replace("_collapsed", "")}</span>
            <span className="loc-file">{c.file}<span className="loc-colon">:</span>{frame.line}</span>
            <span className="full-tag">whole file · executed lines lit</span>
          </div>
          <div className="loc-role serif">{frame.role}</div>
        </div>
        <FullFileView apiBase={apiBase} scenarioId={scenarioId}
                      path={frame.fullFile} activeLine={frame.line}
                      onClose={() => setShowFile(false)} />
      </div>
    );
  }

  return (
    <div className="loc-wrap terrain">
      <div className="loc-head">
        <div className="loc-head-row">
          <span className={"kind-badge k-" + frame.kind}>{frame.kind.replace("_collapsed", "")}</span>
          <span className="loc-file">{c.file}<span className="loc-colon">:</span>{frame.line}</span>
          {frame.loop_count && <span className="loop-chip">↻ ×{frame.loop_count} iterations · collapsed</span>}
          {canFull && (
            <button className="ff-open" onClick={() => setShowFile(true)} title="show the whole source file">
              ⤢ full file
            </button>
          )}
          <button className="ff-open" onClick={doExplain}
                  title="copy a grounded pointer (recorded state + follow-up commands) for an agent that has Cairn">
            ⌘ explain
          </button>
          <button className="ff-open" onClick={doHandoff}
                  title="copy a self-contained bundle (with source) to paste into any harness">
            ⇪ handoff
          </button>
          {copied && <span className="copied-flash">{copied}</span>}
        </div>
        <div className="loc-role serif">{frame.role}</div>
      </div>

      <div className="loc-main">
        <div className="codeblock">
          <div className="codeblock-fn">fn {c.fn} <span style={{ color: "var(--text-dim)" }}>· {c.file}</span></div>
          {sourceless
            ? <ReplayedLines loc={loc} />
            : c.lines.map((line) => (
                <CodeLine key={line.n} line={line} frame={frame} predict={predict} />
              ))}
        </div>

        <aside className="staterail">
          <div className="eyebrow" style={{ marginBottom: 12 }}>Recorded state</div>
          {frame.values.map((v, i) => {
            const best = locVals[v.name];                  // richer line-level decode, if any
            const shown = (best && best.full) || v.full || v.value;
            const refv = (best && best.ref) || v.ref;
            const tree = (best && best.tree) || v.tree;
            return (
              <div className="state-item" key={i}>
                <div className="state-top">
                  <span className="state-name">{v.name}</span>
                  <span className="state-event" style={{ color: EVENT_COLOR[v.event] }}>{EVENT_TAG[v.event]}</span>
                </div>
                {refv && <div className="state-ref"><RefBadge refv={refv} /></div>}
                {tree
                  ? <div className="state-val vtree" style={{ color: EVENT_COLOR[v.event] }}>
                      <ValueTree node={tree} depth={0} openTo={Infinity}
                                 ctx={apiBase && scenarioId ? { apiBase, scenarioId, fid: frame.id } : null} />
                    </div>
                  : structIsTree(shown)
                    ? <pre className="state-val struct" style={{ color: EVENT_COLOR[v.event] }}>{formatStruct(shown)}</pre>
                    : <div className="state-val" style={{ color: EVENT_COLOR[v.event] }}>{shown}</div>}
                {v.as_time && <div className="state-time">⏱ {v.as_time}</div>}
              </div>
            );
          })}
          {frame.io && (
            <div className="io-note">
              <div className="io-note-h">⇄ left the process</div>
              <div className="io-note-b">{frame.io.detail}</div>
              <div className="io-note-w">
                {frame.io.waited_ms != null ? "waited " + frame.io.waited_ms + "ms · " : ""}internals not recorded
              </div>
            </div>
          )}
        </aside>
      </div>

      {/* counterfactual summary for any branch — hidden while a prediction is
          still in doubt so it can't spoil the answer */}
      {b && !(predict && !predict.answered) && (
        <div className="counterfactual">
          <div className="cf-col cf-taken">
            <div className="cf-label">▸ took</div>
            <div className="cf-text">{b.taken_arm}</div>
          </div>
          <div className="cf-divider" />
          <div className="cf-col cf-not">
            <div className="cf-label">⌁ road not taken</div>
            <div className="cf-text">{b.not_taken.summary}</div>
          </div>
          <div className="cf-decide">
            decided by <span className="val">{b.deciding_value.name} = {b.deciding_value.value}</span>
          </div>
        </div>
      )}

      {/* predict invitation lives here when a surprising fork is in doubt */}
      {predict && predict.frame === frame.id && (
        <PredictPrompt branch={b} predict={predict} onPredict={onPredict} />
      )}
    </div>
  );
}

Object.assign(window, { LocView, EVENT_COLOR });
