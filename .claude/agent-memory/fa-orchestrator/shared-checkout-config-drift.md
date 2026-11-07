---
name: shared-checkout-config-drift
description: The gate reads .foragent.yml from the shared checkout; when it lags main, runs use old team settings (11-07 ci_shards 4 vs 6)
metadata:
  type: project
---

On 2026-11-07 03:57Z #2161 was blocked "acceptance-baseline-unavailable: shard_count observed 6, expected 4". main had set `ci_shards: 6` (#2460), but the shared checkout /Users/apple/Dev/foragent was still at a8ed26fd, and the gate's load_config reads `.foragent.yml` from there.

**Why:** nothing moves the shared checkout to main automatically; my pull-main.sh (ff-only) did it only when I published.

**How to apply:** when a block names a config value that disagrees with main, run `git -C /Users/apple/Dev/foragent log -1` against origin/main first, fast-forward with drafts/bin/pull-main.sh, then retry. A structural fix is recorded as a next-order idea (gate reads team config at the run's main revision). Related: [[publish-races-spec-removal]].

11-08 01:59 KST: right after I changed .foragent.yml roles in the shared checkout (`fa role set architect`, drop lane_concentration), all nine prepared statement-route publications came back "review is for a different requirement, contract, reference or writer/reviewer setup" (prepare identity hashes the setup). Likely cause (not proved: #2601/#2614 also merged then). Rule: change .foragent.yml only when no reviewed publication is pending, or expect to re-prepare every one.
