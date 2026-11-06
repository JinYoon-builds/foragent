---
name: broken-dashboard-once-fixed-width
description: fa dashboard --once always prints a 120-column frame, even in a 40-column pane (confirmed 2026-11-07)
metadata:
  type: project
---

Goal: read the dashboard once in a phone-width pane.
Start: foragent 0.1.0 (163033ba), a 40- or 80-column terminal.
Repro: `fa dashboard --once --repo-root <repo>` in a pty sized 40 cols (COLUMNS=40 also ignored); measure the widest line.
Observed: always 120 cells; squirrel and bubble side by side, no narrow stacking.
Live dashboard not tested: a second window closes itself while one is already open.
Confirmed by second session 2026-11-07 (40 and 80 cols both 120 wide, identical 65-line frame); re-reported via fa orch request. Retest: --once widest line <= pane cols.
Tracked as request r898. Retested 2026-11-07 after order #2294 (#2078 dashboard redesign): still 120 cols at 40; no new report needed. A local clone counts as the same repo, so the live view cannot be opened beside a running one.
