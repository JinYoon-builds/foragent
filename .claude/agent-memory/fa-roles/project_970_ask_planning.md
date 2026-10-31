---
name: project-970-ask-planning
description: #970 typed fa orch ask (question/merge/order), planning handoff, statusline plan-heartbeat renewal, watch --follow inbox events
metadata:
  type: project
---

- `orch_inbox.ask(..., kind=)` stores `kind` (QUESTION_KINDS); `ask_once` writes `kind: "question"`. CLI `_orch_box._print_ask` prints the planning handoff (messages/orch_inbox.py, en+ko; the ko line must contain the literal word "planning" because the locked test lowercases and searches for it) or the push note; `--json` adds `route`.
- Statusline renewal: `presence.renew_planning(Path.cwd())` from `statusline.run` after the NESTED_ENV return. Skips when `FORAGENT_ROLE` is in env at all, or cwd is under `<state>/worktrees` (same rule as `watch_open.record_terminal`). `orch_inbox.renew_plan_heartbeat` only rewrites an existing file older than 60 s. Fixed answer from Jin: add no new hook and leave fa init unchanged.
- `orch.watch(follow=True)` uses `orch_inbox.FollowEvents(store)`. Take the baseline in the constructor, when the watch starts. Do not take it at the first successful poll: a reviewer found that records written while early reads fail would be lost. If the start read fails, only records stamped before the start count as seen. It then emits `{"event":"request"|"answer",...}` lines after the snapshot. Progress notes stay quiet.
- SKILL.md is capped at 150 lines. A rewrap that keeps backtick spans as single tokens at width 118 freed lines. Check: before and after the edit, the 274 tests in the acceptance files that read the skill gave identical PASSED/FAILED sets.

**Why:** These facts cannot be read from the diff: the fixed product answers and the test traps.
**How to apply:** When you extend inbox routing, the statusline, or skill text, reuse these seams.
