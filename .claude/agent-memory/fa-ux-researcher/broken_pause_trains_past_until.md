---
name: broken-pause-trains-past-until
description: fa orch pause-trains with a past --until prints "paused", exit 0, but no pause is in effect (confirmed 2026-11-07, after #2578)
metadata:
  type: project
---
Goal: pause the merge owner's train building with an end time.
Start: scratch repo with origin, .foragent.yml lanes + merge_owner: true, checks: ci, merge_batch: true, test_command: uv run pytest.
Repro: `fa orch pause-trains --why x --until 2020-01-01T00:00 --repo-root $S` → "Train building paused ... (until 2020-...)" exit 0; then `fa gate status` has no train line, --json train_pause null, `fa orch resume-trains` says "was not paused".
Expected: refuse or say already over, non-zero exit (like bad 'tomorrow' → exit 2).
Status: CONFIRMED; reproduced 2026-11-08 (562e8ffa). The 2026-11-07 report never reached the inbox; re-reported 2026-11-08 (state "reported"). Also noted: pause not shown on fa dashboard.
**How to apply:** re-run the repro; remove this entry once a past --until is refused.
