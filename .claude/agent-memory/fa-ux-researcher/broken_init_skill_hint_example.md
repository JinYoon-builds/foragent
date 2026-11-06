---
name: broken-init-skill-hint-example
description: fa init's ambiguous-skill hint gives as its example the skill setup already chose, and names a lane for a role default skill
metadata:
  type: project
---
Goal: a first-time user switches which of two same-named plugin skills setup uses.
Start: two Claude plugins ship one skill name (Jin's Mac mini: pstack and pstack-skills, principle-prove-it-works).
Shortest repro: in a fresh git repo with an origin remote and one commit, run `fa init --dry-run --no-launchd --no-board --no-push`.
Observed 2026-11-06 (6108b37e, rc=0): "...matches several skills (pstack:...); setup uses pstack-skills:... To use another one: fa lanes skills <lane> <id>, for example fa lanes skills <lane> pstack-skills:...". The example repeats the chosen skill, so copying it changes nothing. It is a role default, but the hint names a <lane>. Confirmed by a second session 2026-11-06 (same output, rc=0) and reported to the orchestrator. `fa lanes skills --help` only shows `name`, so it is unclear whether a role name works there.
Rerun 2026-11-06 after order #2242 closed (ed15ca1a): same line. Also the pick contradicts #2243's 'first in name order' (pstack sorts first, pstack-skills is picked). r882 accepted but no issue; follow-up reported 2026-11-06 (waiting for confirming session, owner orchestrator).
**How to apply:** rerun on the next visit; delete once the example names the other candidate.
