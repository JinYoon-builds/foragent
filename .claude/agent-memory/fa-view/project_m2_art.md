---
name: m2-art-1059
description: #1059 watch_art ansi_rows lower-half fix, lookout/lying frames, and why `uv run pytest tests` cannot pass before sibling issues
metadata:
  type: project
---
- `ansi_rows`: a bottom-only cell becomes `▄` with only `38;5;<lower>`. `half_blocks` data stays unchanged (D64). presence/statusline draws the big squirrel through `ansi_rows`.
- `lookout(tick)` and `lying_asleep(tick)` build frames as a base grid plus palette-key points (`_frame`). Working/yellow stem rocks `◖◜◖◟`; all other styles keep `◖`.
- The Done-when `uv run pytest tests` includes fixed tests owned by #1062 (test_lane_m2_blocks, test_squirrel_running, …) and #1064. Those issues are blocked by #1059, so they fail on the base too.
**Why:** a full run takes over 10 minutes under machine load and times out.
**How to apply:** check for regressions by running the related files with `PYTHONPATH=<repo>/.pytest_cache/base/src` (from `git archive HEAD src`) and diffing the FAILED IDs. `.pytest_cache/` is gitignored and lives inside the repo, but `.foragent/` is not gitignored.
