# Refactoring

Source: adapted from `pstack/playbooks/refactoring.md` (pstack by Lauren Tan, MIT License).

A refactor changes structure, not behavior. If the cleanup reveals a missing feature
or a real bug, split it out into its own issue and ship the structural change first.

1. Reproduce the current behavior before anything moves: run the existing tests and
   the real command, and record their output as the baseline.
2. Pin the behavior contract. Where coverage is missing, write a characterization or
   equivalence test first; it must fail when you break the behavior on purpose, before
   you change the code. Type checks and lint are not a pin.
3. Rule out hypotheses about the contract with evidence. Do not guess what callers rely
   on: search every caller, read the code, and when a dependency is unclear, add logging
   or instrumentation and observe the runtime values it reveals.
4. Plan the target shape from that evidence: the module layout, types and call graph
   you would build today. The reshape must delete branches or invalid states, not add
   indirection. Subtract before you add: remove dead code and one-caller wrappers first.
5. Move in small steps that keep the pin green. Migrate every caller and delete the old
   API in the same change. A module split is the exception: it keeps a compatibility
   facade (see "Splitting a module" below). Check renames against strings and docs.
6. Confirm that behavior is unchanged on the same surface as the baseline: rerun the same
   tests and command and diff the output. "It compiles" is not proof.
7. Keep the change only if it lowers reader load somewhere; otherwise revert it.

Report the result with evidence: the structure that changed, the pin it held against,
the before-and-after output, and what shipped and what was reverted.

## Architect proposals

`fa orch architect --why "<evidence>"` starts one architect session that follows this
procedure and only proposes; the orchestrator decides. Never guess where the pain is.

1. Measure first with `fa orch facts`: the hot files by overlap records and waits, the
   co-changed files and the line counts. Measure slow tests with `pytest --durations=25`.
2. One proposal per file or concern. Each proposal cites those numbers or a real incident
   (date and what happened) and states its expected measurable effect: which number moves,
   from what to what, and how to measure it again.
3. Mark each proposal `Decision: orchestrator` or `Decision: person` and send it with
   `fa orch request --from architect --text "<proposal>"`. Person decisions are lane
   boundary changes, anything irreversible, scope and taste; the orchestrator forwards them
   as multiple choice and decides the rest. The architect never publishes or approves orders.
4. "Nothing to change" is a valid and often the best outcome. Report it with the numbers
   that showed no clear gain.

Every proposal and every review checks the state-ownership rules:

- One writer per piece of state. Others leave a request for the owner (for example a stop
  request) and never write it themselves; a fallback writer acts only after the owner is
  confirmed dead.
- Derive instead of store when the value can be computed from recorded facts (for example
  sleep credit from sleep and wake points).
- Append instead of overwrite (runs.jsonl style).
- Flag duplicated state (for example GitHub labels versus the local record) and name the
  reader that tolerates the lag.

## Splitting a module

1. A split preserves behaviour through a compatibility facade, as in the cli.py, state.py,
   scheduler.py and run_publish.py splits: every module-level name of the old module stays
   importable from it, and existing patches on the old module still reach the code (the new
   modules look names up through the facade at call time).
2. Each split order carries a line-limit acceptance test and reserves the file with
   `exclusive:` while it runs.
3. Last step: list the open issues whose Files name a moved path and update their Files
   (the orchestrator does this through order edits and a preread). The old module names stay
   importable through the facade until those issues merge.
