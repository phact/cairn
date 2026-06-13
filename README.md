# Cairn

Cairn records a **real execution** of your program, ranks what mattered, and
serves it at three zoom levels — so a developer (or an AI agent) can rebuild a
mental model of code they didn't write from what *actually ran*, not from
guesses about it.

**The one rule:** Cairn never reasons. Every output is recorded ground truth,
or a static counterfactual explicitly labeled as such. It never calls a model,
never narrates, never fills a gap with "probably." Narration is the consumer's
job (a human, or an agent driving the CLI); Cairn's job is to make the truth
cheap to get.

```
tier 1  precis     what this run did to the world, one paragraph of facts
tier 2  skeleton   the ranked salient path + causal edges (the swimlane)
tier 3  loc        line-by-line execution with decoded state + reverse-step
```

## Quickstart

```bash
bin/cairn record --crate <crate> --test <test_name>     # rr-record + derive
bin/cairn status                # active trace, backend, capabilities
bin/cairn precis                # tier 1
bin/cairn skeleton              # tier 2 — the numbered salient path
bin/cairn explain 5             # orient on frame 5: branch + recorded state
bin/cairn loc 5                 # tier 3 — line-by-line with values
bin/cairn value 5 --path '["req","headers","[0]"]' --instant 13   # follow a pointer
bin/cairn open 5                # the browser workspace at that frame
bin/cairn watch                 # run the local server (API + workspace, :8787)
```

Python flows record the same way: `bin/cairn record --lang python
--script <entry.py> --src <dir>`. Every read command takes `--json` (the same
artifact schema the REST API and UI consume) and `--trace <id>` to override the
active trace; every response echoes the trace id it answered from.

## The grounding contract

These rules are load-bearing — agents and UI key behavior on them:

- **Recorded vs static.** Values carry their event (`read`/`born`); a branch's
  `not_taken` arm is the *static* alternative, labeled with provenance, and
  described with "would", never "did".
- **Lossy values are explicit, in two distinct ways.** A clipped scalar is
  visibly marked `…[clipped]`. An **unexpanded pointer** is the dangerous one —
  it looks like a value but wasn't walked — so every pointer-like node carries
  `deref.status`: `unexpanded` (real, retrievable, not walked — the ONLY state
  with an expand affordance, and it carries the exact `cairn value … --instant N`
  command to follow it), `expanded`, `null` (nothing there — a fact), or
  `unreadable` (tried; gone). Consumers key on `status`, never on shape.
- **Deterministic decodes are additive, never replacements.** bytes→UTF-8
  (`ascii`), JWT→claims (`jwt`, signature *not* verified — we read state, we
  don't assert validity), epoch→ISO (`as_time`, gated on the *name* — range
  alone would mislabel counts as dates), enum discriminants (`variant`). Raw
  values stay; flags mark the derived reading. Specs live in one place
  (`cairn/decoders.py`) so backend copies can't drift.
- **Machinery collapses structurally, not by crate list.** A no-printer struct
  containing an `UnsafeCell<T>` is an interior-mutability wrapper: show the
  guarded `T` (`Mutex(Vec(…))`, `RwLock(None)`), elide the semaphore/waitlist
  guts. Works for std/tokio/parking_lot/hand-rolled locks alike.
- **When a capability is missing, the API says so** (`capabilities` in every
  artifact) rather than faking it. Example: the Python backend refuses `value`
  expansion with the reason (eager capture; the process is gone).

## CLI (`bin/cairn`) — the substrate

A future MCP server only wraps this; the Skill only teaches an agent to drive it.

| command | does |
|---|---|
| `record` / `derive` / `traces` / `use <id>` / `status` | capture + trace lifecycle (`derive` rebuilds `derived/` from the recording — byte-identical, proven) |
| `precis` / `skeleton [--threshold]` | tiers 1–2 |
| `frame <n>` / `state <n>` | one salient frame; its decoded recorded state |
| `loc <n>` / `step <n> --instant N` | tier 3; re-read state at a replay instant |
| `value <n> --path --instant --depth` | dereference an unexpanded pointer *as it was at that instant* |
| `sources` / `source --file <f>` | recording-touched files, with executed-line sets |
| `explain <n> [--question]` | **pointer** for agents: location + branch + this frame's recorded state + follow-up commands. No prose, no source. |
| `handoff <n> [--question]` | **bundle** for unprimed receivers: self-contained prompt *with* a source slice |
| `diff <other>` / `bench` | divergence between two recordings; the ranker benchmark |
| `predict log …` | append a prediction outcome (cross-trace, keyed by frame kind) |
| `open [n]` / `watch` | browser workspace bridge; the local server |

Exit codes: `0` ok · `2` no active trace · `3` not found · `4` recording failed.
`SKILL.md` carries the agent teaching (the grounding discipline).

## Server + REST (`cairn watch`, :8787)

Same engine, same schema, for the browser workspace:

```
GET  /status                                   active trace + all traces (discovery)
POST /record                                   run the pipeline
GET  /scenario/:id                             full artifact
GET  /scenario/:id/precis|skeleton|sources     tiers 1–2, file list
GET  /scenario/:id/source?path=                whole file + executed lines
GET  /scenario/:id/frame/:fid/loc[/step/:n]    tier 3 (+ instant re-read)
GET  /scenario/:id/frame/:fid/value?path=&instant=&depth=    lazy expansion
GET  /diff?a=&b=                               divergence + ranker check
POST /scenario/:id/predictions                 predict-mode logging
```

404s carry `have` + `active_trace` so the UI never guesses ids. Tier-3 is
served by a warm `rr replay` session pinned to one recording — it re-pins when
the requested trace changes and **refuses loudly** rather than serve state from
the wrong recording. Results are cached (`derived/loc_cache/`, keyed by
recording + decoder mtimes), and salient frames are precomputed at startup:
cached tier-3 ≈ 2 ms.

## Browser workspace (`web/`)

The visual side of the same contract — a **build-step-free** frontend (React +
Babel from a CDN, JSX transpiled in the browser; no bundler, no `node_modules`).
`cairn watch` serves it next to the API on :8787 and `cairn open [n]` launches
it at a frame; for frontend dev, `web/serve.sh` runs a standalone static server
that reverse-proxies `/api/*` to the backend (same-origin, no CORS).

**Live-only, never mock.** It discovers the active trace from `GET /status` and
renders a real recording — or an explicit "no live trace" with a retry. It never
substitutes demo data; missing state says so. Capture backend is transparent:
the same UI renders `rr-dwarf` (Rust) and `py-monitoring` (Python) unchanged.

The three tiers, made navigable:

- **Summary** — the précis prose verbatim; the function names it mentions become
  grounded links. `←/→` move a selection cursor across them, `Enter` drops into
  that frame.
- **Skeleton** — the swimlane: lanes in stable source order (collapsible per
  module, with a collapse/expand-all toggle), the numbered salient path, I/O
  excursions, road-not-taken stubs. Click a node or step the playhead — stepping
  stays in-tier; `Enter` dives to the code.
- **LOC** — real source with executed lines lit and DWARF state inline; the
  **recorded-state rail** renders the value tree.

The grounding contract *is* the UI's logic, not decoration:

- The **value tree** keys the dereference affordance strictly on `deref.status`
  — only `unexpanded` shows a `deref →`, and clicking it calls
  `/…/value?path=&instant=` to follow the pointer *as it was at that instant*.
  `null` / `unreadable` / `expanded` are terminal and shown distinctly (never a
  fake expand control on a node with nothing behind it).
- **Decoders** render where the backend flags them — `jwt` as decoded
  header + claims, `as_time` as a clock-stamped time, bytes-as-string, enum
  `variant` — additive to the raw value, with the full value on hover.
- **Machinery** arrives pre-collapsed (`Mutex(Vec(…))`, `RwLock(None)`), and a
  pointer shows a demoted `ptr 0x…` badge beside its clean contents.
- **explain / handoff** buttons copy the spec's two artifacts: a *pointer*
  (recorded state + follow-up commands, for an agent that has Cairn) or a
  self-contained *bundle* with a source slice (for an unprimed harness).

Keyboard: `←/→` (or `d`/`f`) step · `↑/↓` (or `k`/`j`) zoom tiers · `Enter`
advance-and-dive · `Esc` skip a predict invite · click any node / clause /
highlight to jump. Plus a full-file view (`⤢`), predict mode, and a record panel
(pick test + mode → `POST /record`).

## Backends — composition, one contract

Language backends are plain adapter objects in a registry
(`cairn/backends.py`), dispatched by the artifact's declared
`capture_backend`. They share the contract (normalized FrameStream, value
schema, capabilities), not implementation:

| | Rust (`rr-dwarf`) | Python (`py-monitoring`) |
|---|---|---|
| capture | rr record; gdb replay-extract (DWARF + Rust pretty-printers) | `sys.monitoring`; eager per-line state for first N activations |
| tier 3 | live warm replay session (true time travel) | index into eager recording |
| reverse-step | native | indexed re-read |
| `value` expansion | live, time-indexed | honestly refused |

Cairn dogfoods itself: `python::trace_rank` is Cairn's own ranker traced by the
Python backend. Deep dive: `ARCHITECTURE.md`.

## On-disk layout (`.cairn/`)

```
.cairn/
  config.toml                   active_trace pointer
  project.toml                  per-project capture settings (see below)
  predictions.jsonl             PERSISTENT, CROSS-TRACE (keys on frame_kind)
  traces/<scenario-id>/
    recording/                  the rr recording — single source of truth
    derived/                    rm -rf safe; `cairn derive` rebuilds byte-identical
  mining/                       commit-pair mining workspace (clone + built bins)
```

Scenario id = trace id (`crate::test`, `python::<script>`, `pr-<label>::<test>@<sha>`).

### Pointing Cairn at your code

The engine names no scenario. Per-project specifics live in
`.cairn/project.toml` (gitignored), so nothing repo-specific is baked into the
committed source:

```toml
binary       = "bin/<your-test-binary>"   # the compiled rr-recordable test bin
crate        = "<your-crate>"
default_test = "<a representative test>"
src_root     = "../your-repo"             # for source display (relative to repo root)
user_mark    = "<your-crate>/src/"        # which recorded paths are "your code"
files        = "a.rs,b.rs"                # optional: narrow capture to these files
strip_prefix = "<crate_symbol>::"         # trimmed from demangled names for readable lanes
```

Any of these can also be passed per-invocation (`--crate`, `--user-mark`,
`--files`, `CAIRN_SRC_ROOT`, …); env and flags win over the config.

## Trace diff, the benchmark, and mining

Two recordings of related code align positionally (never semantically) and
diff on three channels — **flips** (same activation, different recorded arm:
the deciding frames, ground truth by construction), **presence** (ran in only
one run: the road that became real), and **path** divergence. Flips subsume
their mechanism (a predicate flip folds into the deciding caller that invoked
it); count-only differences are consequences, not labels.

`cairn bench` scores the ranker against these free labels (per-side MRR /
hit@K / rank-percentile / per-pair macro recall). This is how ranker changes
stopped being vibes: fan-in earned its keep here (+8pp macro recall); a
contingency signal and an LLM-as-judge scorer were both measured and rejected
here.

**Mining history** (`scripts/mine_pair.py <label> <shaA> <shaB> [--test …]`)
turns any commit pair into traces + labels. The empirical law from mining this
repo's history, worth knowing before you dig:

> Green⇄green pairs (refactors, reverts, schema changes) yield presence
> labels, count signatures, and negative controls — a comments-only commit
> provably produces an empty diff. They structurally **cannot** yield flip
> labels, because both sides pass. Flips need a **red run**. Red runs from
> history need graft-compatible regression tests, which co-evolving
> tests-and-types mostly block — and old commits hit a toolchain bitrot
> horizon anyway.

### Capture on CI failure — where flip labels actually come from

The consequence of that law: **record the bug while it exists.** Built and
proven end-to-end (`cairn/capturefail.py`):

```
cairn capture-on-fail -- cargo test -p <crate>          # local, and the CI step
cairn capture-fix fail-<sha>::<test>@red                # after the fix lands
cairn --trace fail-<sha>::<test>@red diff <crate>::<test>@green   # the flip signature
```

1. **Green: transparent.** The wrapper streams the test command's output through
   unchanged and exits with its code — ~zero overhead while tests pass.
2. **Red: record.** For each failing test (parsed from cargo's output), re-run
   *just that test* under `rr record`, derive, and tag `fail-<sha>::<test>@red`.
   The wrapper preserves the failing exit code, so CI still goes red.
3. **Honest tags.** If the isolated re-run *passes* (didn't reproduce out of
   suite context), it's tagged `@flaky`, never presented as the bug. If rr
   records nothing, it says so.
4. **`capture-fix` after the fix** records `…@green`; `cairn diff` of the pair
   is the bug's **flip signature** — the deciding frame, by construction, with
   zero grafting and zero bitrot. Demonstrated: a flipped comparison surfaced as
   `token_is_valid` returning `false`@red vs `true`@green at activation #0.

**Per-repo scope.** The Rust extractor's user-code filter comes from the
project config (below) or `--user-mark <path-substr>` / `--files <a.rs,b.rs>`;
`capture-fix` reads them back from the red trace's metadata so you set them
once. CI: swap the test step for the wrapped version and upload
`.cairn/traces/fail-*` (a few hundred MB) as a build artifact.

Every future bug becomes a benchmark label and a debuggable recording at the
moment it exists. The red recording is immediately useful on its own:
`cairn explain` on a failing CI run is the spec's core "why did this run do X"
question, answered with recorded values instead of a rerun-and-printf loop.

## Environment

- x86-64 Linux, `rr` on PATH, debug builds (DWARF). `kernel.perf_event_paranoid ≤ 3`.
- **AMD Zen**: apply the SpecLockMap workaround once per boot or `rr replay`
  silently diverges: `sudo python3 scripts/zen_workaround.py`.
- Python backend: CPython ≥ 3.12 (`sys.monitoring`), no rr needed.

## Repo map

```
cairn/            core engine: layout, rank, artifact, decoders, sources, errors,
                  backends (registry), pipeline, tracediff, bench, api, cli, pyloc
backends/rust/    extract.py, loc_server.py (gdb agent), rrloc.py, spans.py, otherarm.py
backends/python/  capture.py (sys.monitoring + eager LOC), drivers
web/              browser workspace: index.html, app + tier views (jsx),
                  adapter + loader (live wiring), serve.py (static + /api proxy)
bin/cairn         CLI launcher        scripts/mine_pair.py   miner
SKILL.md          agent teaching (grounding discipline — the soul of the tool)
ARCHITECTURE.md   multi-language design, bitter-lesson roadmap, benchmark history
spec.md           original backend spec (grounding principles, tier contract)
```

## Known limitations (kept honest)

- One process per recording; multi-process flows (forwarder/two-box paths) are
  out of scope for now.
- `not_taken.target_line` is filled only when static analysis finds the real
  other-arm site (Rust only; capability-gated) — otherwise withheld, never guessed.
- Python eager capture can't expand values post-hoc (clipped nodes are
  `unreadable`, not `unexpanded`) and records only the first N activations per
  function.
- The ranker is near its handcrafted ceiling on current labels (measured); the
  path up is label volume — see capture-on-fail above.
