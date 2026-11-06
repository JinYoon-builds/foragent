---
name: broken-stop-no-person-release
description: fa stop is a person command, but its undo (fa orch release) is hidden under internal orch; help never names it
metadata:
  type: project
---
Goal: a person without a PM stops FA with `fa stop`, then restarts it later.
Start: foragent 0.1.0 (ed4149f6), after #1491/#1492 merged.
Shortest repro: `fa --help` lists only `stop` under Person commands; `fa stop --help` has no restart hint; the undo is `fa orch release` under the internal `orch` group.
Observed: the restart step is not discoverable. Reported 2026-11-06 (request r820), re-verified after order #1740 the same day, unchanged; help-text part confirmed by a second session the same day. Agent sessions cannot run `fa stop` ("Foragent started this session"), so the post-stop output still needs a check from a person terminal. r820 accepted 2026-11-06: help part ordered (fa stop --help to name fa orch release or /fa); a separate person resume command was declined (Jin q7). Re-checked after order #1775 the same day: still missing in ed4149f6, so do not report it again until that help fix ships.
**How to apply:** on revisit, check `fa --help`, `fa stop --help` and the `fa stop` output for a restart step; remove this entry once one is shown. The resume command alone is not a find; it was declined.
