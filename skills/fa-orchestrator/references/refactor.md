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
   API in the same change; no compatibility shims. Check renames against strings and docs.
6. Confirm that behavior is unchanged on the same surface as the baseline: rerun the same
   tests and command and diff the output. "It compiles" is not proof.
7. Keep the change only if it lowers reader load somewhere; otherwise revert it.

Report the result with evidence: the structure that changed, the pin it held against,
the before-and-after output, and what shipped and what was reverted.
