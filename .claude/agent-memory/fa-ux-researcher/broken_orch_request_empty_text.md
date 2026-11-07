---
name: broken-orch-request-empty-text
description: fa orch request --text - with empty/whitespace stdin saves an empty report and exits 0 (confirmed 2026-11-07)
metadata:
  type: project
---
Goal: deliver a role find with the taught `--text - <<'EOF'` form.
Start: ux-researcher session (FORAGENT_ROLE_REPORTS set as `<role>=<path>`), cwd /Users/apple/Dev/foragent.
Shortest repro: `fa orch request --from ux-researcher --text - </dev/null` (also `--text "   "`, or an empty heredoc).
Observed: `Request report saved (reported).`, exit 0, `{"text": ""}` in the reports file. Expected an error and a non-zero exit.
Reported 2026-11-07 after order #2262; reproduced again by a confirming session on 2026-11-07 and sent as CONFIRMED (no orch inbox item yet at that time). Recheck: rerun the repro and expect a non-zero exit; remove this entry when it errors.
Testing tip: run empty-input tests only with FORAGENT_ROLE_REPORTS pointing at a temp file (`ux-researcher=<tmp>/r.jsonl`). A bare path is silently ignored and the request goes to an unknown store.
