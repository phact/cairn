---
name: cairn
description: >
  Use when the user wants to understand how a Rust or Python program actually
  executed — onboarding into an unfamiliar flow, verifying AI-written code,
  answering "why did this run do X / take this branch / return this value", or
  understanding a failing test. Cairn records a REAL run and serves recorded
  ground truth at three zoom levels. Trigger on: "trace this", "why did
  <function> return/branch/do X", "walk me through what actually happened",
  "what was <variable> at runtime", "help me understand this flow/handler/test",
  "why did this test fail". Do NOT use for static questions answerable by
  reading source alone, or for languages other than Rust and Python (v0).
---

# Cairn

Cairn serves **recorded ground truth** from a real execution. Your job is to
explain that truth — never to guess what code probably did.

## Core discipline (non-negotiable)

- Explain ONLY from values Cairn returns. Cite frame numbers and actual recorded
  values ("at frame 5, `wanted` was [104,111,115,116] = \"host\"").
- If you need a value you don't have, run a Cairn command to get it. Do NOT infer
  it. If it isn't recorded, say so plainly.
- A `not_taken` branch is the STATIC alternative arm. Describe it with "would"
  ("the not-found arm would return None"), never "did".
- Never present your interpretation in the same breath as a recorded value
  without making clear which is which.

### Check capabilities first

`cairn status` reports the backend and a `capabilities` block. Backends differ,
and the block is the truth — never assume a feature is present:

- **Rust (`rr-dwarf`)**: full time travel. `reverse_step`, `value` pointer
  expansion, and static `counterfactual` target lines all work.
- **Python (`py-monitoring`)**: state was recorded EAGERLY at capture; there is
  no live debugger to re-query. `loc`/`step` read the recording, but `value`
  expansion is refused (the process is gone) and `counterfactual` is absent.
  Honor the refusal — do not work around it.

### Lossy values — two cases, handle them differently

A value Cairn returns may be incomplete in one of two ways. Never treat either
as the whole truth.

- **Clipped scalar** (marked `…[clipped]`): identifiable but truncated. Expand
  with `cairn value` only if the elided tail bears on the question.
- **Pointer node** — key STRICTLY on `deref.status`, never on the node's type or
  whether it shows children:
  - `unexpanded` — recorded but not walked (a budget decision, not a fact). The
    ONLY dereferenceable state. Do NOT describe shape/length/contents past it and
    do NOT treat it as a leaf; run its `deref.expand` command (the node carries
    the exact `cairn value <n> --path <p> --instant <N>`) or say it wasn't
    followed. *(Rust only — Python can't expand; there it won't appear.)*
  - `unreadable` — tried, gone (or, on Python, clipped by the eager depth budget
    and not retrievable). Terminal. A fact, not a thing to expand.
  - `null` ("nothing there") and `expanded` ("already walked", including a
    genuinely empty collection) — terminal facts.

  This is the data-side equivalent of never narrating a branch that didn't run.

## Workflow

1. **Ensure a recording exists.** `cairn status`. If none:
   - Rust: `cairn record --crate <c> --test <t>` (prefer an integration/e2e test
     for honest I/O; unit tests mock the edges).
   - Python: `cairn record --lang python --script <entry.py> --src <dir>`.
2. **Start wide.** `cairn precis` — what the run did to the world, one paragraph.
3. **Get the path.** `cairn skeleton --json` — the ranked salient frames + causal
   edges. The spine of the explanation.
4. **Drill to the frame that matters.** `cairn frame <n> --json` and
   `cairn state <n> --json` for the recorded values and the branch decision.
5. **Mechanism, if asked.** `cairn loc <n> --json` for line-by-line with values;
   `cairn step <n> --instant N` to re-read state at a replay instant.
6. **Follow pointers before describing structure (Rust).** If a value shows
   `deref.status == "unexpanded"`, run its `deref.expand` command at the relevant
   instant, repeating until resolved.
7. **Answering "why".** Find the deciding branch on the path, pull its
   `deciding_value` and `recorded_state`, and contrast the taken arm with the
   `not_taken` (static) arm.

## Orientation shortcut

`cairn explain <n> [--question "..."]` returns in one call: where you are, the
branch, THIS frame's recorded state (the part you can't get from source), and
follow-up commands. Anchor with it, then pull more. It does not write the
explanation — that's your job, grounded in what it returns.

## Comparing two runs / explaining a difference

When the question is "why did B do X when A didn't" (a failing vs passing test,
two scenarios, a before/after), `cairn diff <trace-b> --trace <trace-a>` returns
the **deciding frames** (same call, different recorded arm — the cause),
presence divergence (the road that became real), and the path split. These are
ground truth by construction; lead with them.

For a failing test specifically: a `@red` trace from `cairn capture-on-fail`
carries the bug's recorded state. `cairn explain` on it answers "why did this
fail" from recorded values — no rerun, no printf. If a `@green` (fixed) trace
exists, `diff` the pair for the exact flip.

## When the user wants to SEE it

If they want the visual path, the swimlane, or to step interactively, run
`cairn open <n>` to launch the browser workspace at that frame. The terminal is
for text answers; the workspace is for the visual tiers.

## Output etiquette

Prefer `--json` for anything you parse; plain output when showing the user
directly. Quote recorded values verbatim — they are the ground truth your
credibility rests on.
