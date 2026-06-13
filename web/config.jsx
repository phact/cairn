/* Record / configure panel.
   Reads GET /config for the configurable inputs + defaults and available tests,
   lets you pick the test + mode (and override crate/binary/src_root), then
   POST /record to run the capture pipeline. On success the UI reloads onto the
   new scenario. The pipeline is real work (rr record + replay), so the panel
   shows an honest in-progress state and never fakes completion. */

function RecordPanel({ apiBase, onClose }) {
  const { useState, useEffect } = React;
  const [cfg, setCfg] = useState(null);          // loaded /config
  const [form, setForm] = useState(null);        // editable copy
  const [phase, setPhase] = useState("loading"); // loading | ready | recording | done | error
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    fetch(`${apiBase}/config`, { headers: { Accept: "application/json" } })
      .then((r) => r.text().then((txt) => {
        let j = null; try { j = txt ? JSON.parse(txt) : null; } catch (e) {}
        if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
        return j;
      }))
      .then((d) => {
        if (!live) return;
        setCfg(d);
        setForm({
          test_name: d.test_name || "",
          mode: d.mode || "rr",
          crate: d.crate || "",
          binary: d.binary || "",
          src_root: d.src_root || "",
        });
        setPhase("ready");
      })
      .catch((e) => { if (live) { setError(String(e.message || e)); setPhase("error"); } });
    return () => { live = false; };
  }, [apiBase]);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const record = () => {
    setPhase("recording");
    setError(null);
    fetch(`${apiBase}/record`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(form),
    })
      .then((r) => r.text().then((txt) => {
        let j = null; try { j = txt ? JSON.parse(txt) : null; } catch (e) {}
        if (!r.ok) throw new Error((j && (j.error || j.stderr)) || ("HTTP " + r.status));
        return j;
      }))
      .then((d) => {
        setPhase("done");
        // reload onto the freshly recorded scenario
        const sid = d && d.scenario_id;
        const params = new URLSearchParams(window.location.search);
        if (sid) params.set("scenario", sid);
        window.location.search = params.toString();
      })
      .catch((e) => { setError(String(e.message || e)); setPhase("error"); });
  };

  const busy = phase === "recording";

  return (
    <div className="modal-scrim" onClick={busy ? undefined : onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title serif">Record a run</span>
          <button className="modal-x" onClick={onClose} disabled={busy} title="close">✕</button>
        </div>

        {phase === "loading" && <div className="modal-note">loading configuration…</div>}

        {form && (
          <div className={"rec-form" + (busy ? " disabled" : "")}>
            <label className="rec-row">
              <span className="rec-k">Test</span>
              {cfg && cfg.available_tests && cfg.available_tests.length ? (
                <select className="rec-in" value={form.test_name} disabled={busy}
                        onChange={(e) => set("test_name", e.target.value)}>
                  {cfg.available_tests.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              ) : (
                <input className="rec-in" value={form.test_name} disabled={busy}
                       onChange={(e) => set("test_name", e.target.value)} />
              )}
            </label>

            <label className="rec-row">
              <span className="rec-k">Capture mode</span>
              <span className="rec-seg">
                {["rr", "gdb"].map((m) => (
                  <button key={m} type="button" disabled={busy}
                          className={form.mode === m ? "on" : ""}
                          onClick={() => set("mode", m)}>{m}</button>
                ))}
              </span>
            </label>

            <label className="rec-row">
              <span className="rec-k">Crate</span>
              <input className="rec-in" value={form.crate} disabled={busy}
                     onChange={(e) => set("crate", e.target.value)} />
            </label>
            <label className="rec-row">
              <span className="rec-k">Binary</span>
              <input className="rec-in" value={form.binary} disabled={busy}
                     onChange={(e) => set("binary", e.target.value)} />
            </label>
            <label className="rec-row">
              <span className="rec-k">Source root</span>
              <input className="rec-in" value={form.src_root} disabled={busy}
                     onChange={(e) => set("src_root", e.target.value)} />
            </label>
          </div>
        )}

        {error && <div className="modal-err">{error}</div>}

        <div className="modal-foot">
          {busy ? (
            <span className="rec-busy">● recording — running the capture pipeline (rr record → replay → rank). This can take a minute or two…</span>
          ) : (
            <span className="rec-hint">Re-records and re-ranks, then reloads onto the new scenario.</span>
          )}
          <button className="rec-go" onClick={record} disabled={busy || phase === "loading" || phase === "error"}>
            {busy ? "recording…" : "record"}
          </button>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { RecordPanel });
