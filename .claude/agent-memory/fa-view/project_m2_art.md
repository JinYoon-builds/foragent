---
name: m2-art-1059
description: #1059 watch_art ansi_rows lower-half fix, lookout/lying frames, and the removed whole-suite Done when
metadata:
  type: project
---
- `ansi_rows`: a bottom-only cell becomes `▄` with only `38;5;<lower>`. `half_blocks` data stays unchanged (D64). presence/statusline draws the big squirrel through `ansi_rows`.
- `lookout(tick)` and `lying_asleep(tick)` build frames as a base grid plus palette-key points (`_frame`). Working/yellow stem rocks `◖◜◖◟`; all other styles keep `◖`.
- A Done-when `uv run pytest tests` cannot pass while sibling issues' locked tests fail on base. On 2026-10-31 the orchestrator removed it from #1059–#1064 after a QUESTION.md (option 1); the gate's acceptance baseline guards everything else.
**Why:** a full run takes over 10 minutes under machine load and times out, and sibling-owned tests cannot be fixed in scope.
**How to apply:** if a Done when needs the whole suite, compare failing IDs against a base copy (`git archive HEAD src` into the gitignored `.pytest_cache/base`, run with PYTHONPATH). If other open issues own the failures, raise QUESTION.md early. `.foragent/` is not gitignored.
