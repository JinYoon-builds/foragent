---
name: broken-ci-base-cache-misses
description: CI base cache from #1422/PR #2140 almost never hits; main push runs get cancelled or finish after PR runs start
metadata:
  type: project
---
Goal: PR acceptance runs reuse the trusted main base record (#1422, PR #2140), so CI rounds get shorter.
Starting conditions: the foragent repo with its normal merge rate (main pushes about 1 min apart; a main push run takes about 65 min).
Shortest reproduction: `gh run view <PR foragent-acceptance run> --log`, then read FA_BASE_CACHE_KEY and FA_BASE_CACHE_MATCHED_KEY. Compare with `gh cache list --key foragent-acceptance-base`.
Observed failure (2026-11-06, confirmed by a second session): 1 hit in 17 PR runs. In 12 misses, the base SHA's main push run was cancelled. In 4 misses, the record was saved 43 to 65 min after the PR run started.
Re-confirmed 2026-11-06 by a separate session: 44 PR runs 09:03-14:14Z, 2 full hits, 4 partial (some shards), 38 misses. Reported through fa orch request on 2026-11-06. When revisiting, count hits again and remove this entry when most PR runs hit.
