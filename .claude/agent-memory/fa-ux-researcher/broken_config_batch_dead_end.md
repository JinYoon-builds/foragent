---
name: broken-config-batch-dead-end
description: Old .foragent.yml with review: batch blocks dashboard and fa init (even --review fast) with a one-line error naming no file or fix
metadata:
  type: project
---
Goal: an existing user updates and recovers from an old `review: batch` setting.
Start: temp git repo with origin set, `.foragent.yml` = `review: batch`, fa b2bf7363 (after #1641/#2374).
Repro: `fa dashboard --once --repo-root $T`, `fa init --dry-run --review fast`.
Observed: each prints only `foragent: review must be fast or careful`, exit 1; `fa init --review fast` (no dry-run) leaves the file unchanged. No file, value or fix named. Confirmed by a second session 2026-11-07 and reported via fa orch request (re-sent 2026-11-07 with the 4-step repro).
Rechecked after order #1943 ends (2026-11-07, b2bf7363): still broken. Inbox r919 accepted (bundled with r882/r890 init-message order); do not re-send. Next: when that order merges, rerun the 4 steps.
