---
name: principle-sequence-verifiable-units
description: "Apply to multi-step work (sweeps, migrations, runs of similar edits) and to how you stack commits and PRs. Break work into small units that each end in a verifiable state, check each before the next, and order delivery so the sequence proves itself to a reviewer."
---

Source: vendored from `pstack/skills/principle-sequence-verifiable-units/SKILL.md` (pstack by Lauren Tan, MIT License, Copyright (c) 2026 Lauren Tan, source commit 9b93e81). See LICENSE.
Changed: removed `disable-model-invocation: true` so an agent can load the skill after installing it. The text is otherwise unchanged.

# Sequence work into verifiable units

Order work as a sequence of small units, each ending in a state you can check, and don't advance until the current one is green.

**Why:** A break caught at the unit that caused it is cheap to localize. A break caught after a batch is buried, and you have already built further on a broken base. Sequencing those same units into a delivery a reviewer can replay turns "trust me" into "watch it go red, then green."

**Execution.** In a sweep, migration, or any run of similar edits, verify each change before starting the next. Each unit is a before/after bracket: known-good state, one change, run the check, then proceed. Rebase onto clean trunk first so every check measures against the real baseline. When a lever does the edits, the per-unit check is nearly free. Run it anyway.

**Delivery.** Stack commits and PRs in the order that proves the work. The canonical shape is the failing test first, then the fix on top. Other story orders are a subtraction before the reshape, a baseline capture before the treatment, the scaffold before the feature. Each commit lands on its own and the sequence reads as an argument.

The sequencing complement to the **prove-it-works** principle skill, which keeps each check real, and the **build-the-lever** principle skill, which makes the per-unit check cheap.
