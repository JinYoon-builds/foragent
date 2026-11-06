---
name: broken-dashboard-once-fixed-width
description: fa dashboard --once always prints a 120-column frame, even in a 40-column pane (UNCONFIRMED, 2026-11-07)
metadata:
  type: project
---

Goal: read the dashboard once in a phone-width pane.
Start: foragent 0.1.0 (163033ba), a 40- or 80-column terminal.
Repro: `fa dashboard --once --repo-root <repo>` in a pty sized 40 cols (COLUMNS=40 also ignored); measure the widest line.
Observed: always 120 cells; squirrel and bubble side by side, no narrow stacking.
Live dashboard not tested: a second window closes itself while one is already open.
Reported via fa orch request 2026-11-07; second-session confirmation pending.
