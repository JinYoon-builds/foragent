---
name: broken-config-batch-dead-end
description: Old .foragent.yml with review: batch blocks dashboard and fa init (even --review fast) with a one-line error naming no file or fix
metadata:
  type: project
---
Goal: an existing user updates and recovers from an old `review: batch` setting.
Start: temp git repo with origin set, `.foragent.yml` = `review: batch`, fa b2bf7363 (after #1641/#2374).
Repro: `fa dashboard --once --repo-root $T`, `fa init --dry-run --review fast`.
Observed: each prints only `foragent: review must be fast or careful`, exit 1. No file, value or fix is named, and `--review` cannot repair it. Confirmed by a second session 2026-11-07 and reported via fa orch request.
