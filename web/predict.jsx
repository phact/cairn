/* Predict mode — the friction that writes the model into your head.
   Invitational, never modal: it holds the reveal for a beat, but Enter blows
   straight past and Esc dismisses. Only fires at a surprising fork. */

function PredictPrompt({ branch, predict, onPredict }) {
  const answered = predict.answered;
  const choice = predict.choice;
  const correct = branch.correct;
  const gotItRight = choice === correct;

  if (!answered) {
    return (
      <div className="predict-banner asking">
        <div className="pb-rail" />
        <div className="pb-body">
          <div className="pb-head">
            <span className="pb-tag">⌥ surprising fork</span>
            <span className="pb-q serif">{branch.prompt}</span>
          </div>
          <div className="pb-choices">
            {branch.candidates.map((c) => (
              <button className="pb-choice" key={c.id} onClick={() => onPredict(c.id)}>
                <span className="pbc-line">L{c.line}</span>
                <span className="pbc-label">{c.label}</span>
                <span className="pbc-hint">{c.hint}</span>
              </button>
            ))}
          </div>
          <div className="pb-foot">
            Commit, or <span className="kbd">Enter</span> to blow past · <span className="kbd">Esc</span> to skip predictions here
          </div>
        </div>
      </div>
    );
  }

  // revealed
  const declined = choice == null;
  return (
    <div className={"predict-banner revealed" + (declined ? " declined" : gotItRight ? " right" : " wrong")}>
      <div className="pb-rail" />
      <div className="pb-body">
        <div className="pb-head">
          <span className="pb-verdict">
            {declined ? "↪ skipped" : gotItRight ? "✓ you predicted it" : "✗ not what happened"}
          </span>
          <span className="pb-truth">
            actually fired: <strong>{branch.candidates.find((c) => c.id === correct).label}</strong>
          </span>
        </div>
        <div className="pb-reveal serif">{branch.reveal}</div>
        <div className="pb-decide">
          <span className="pb-dlabel">deciding value</span> <span className="val">{branch.deciding_value.name} = {branch.deciding_value.value}</span>
          <span className="pb-cont"><span className="kbd">Enter</span> to continue</span>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { PredictPrompt });
