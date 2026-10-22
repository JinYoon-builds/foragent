# D57 add. 4, D62 add., D63, D64 (add. 1–6), D68, D70, D71, D72 (add. 1–2), D55 and two safety-check fixes

Decisions: docs/decisions.md. Sketches: docs/design/lane-view-sketch/. Out: D65 README (after the squirrel), D58 addendum 3.

## Shared rules
- People are asked only five kinds of things (D71); answerable things are done and reported.
- Watchers and the lane view read local records only (#138/#139); the squirrel view, progress words, issue boxes and
  settings make zero GitHub requests.
- No LLM call exists only to produce a bubble or a setting; agents report facts with commands (`fa order progress`,
  `fa orch note`, `fa orch ... --say`) and the view fills sentence templates (D64 addendum 3, D70 addendum).
- Animation follows facts (gnawing only while the worker log grows) and can be turned off; text mode carries the same facts.
- Settings live in three scopes: team (.foragent.yml), me (user config), this computer (machine file).
- Design sessions sit beside the orchestrator, are not watched by the gate, and hand off HANDOFF.md (D72).
- Safety-check misfires found on 2026-10-23: changed files compared with current main (#111 held), premerge
  counting tests that also fail on the base (every PR "tests fail" while orders are open), and GitHub listings that
  intermittently return [] (watch reported no blocked issues; publish could not find issues).

## Order of work (GitHub blocked-by links to existing issues)
- orch: #138 → changed-files-base, premerge-baseline → #143 (runner split) → claude-sessions → …
- squirrel-running after #132, #108, #115; progress-words after #139; orch-note/orch-panel after #108;
  settings-cmd after #107; settings-window after #107 and #115.
- When D69 lane clones land (#145), the view lane can run non-overlapping issues side by side; `files` will be
  added to these issues by an edit once #144 lands.

## After merge (not issues)
- D63 proof: run order → implement → review → merge with Claude only on the sandbox repo.
- D67 kill test on the sandbox repo.
