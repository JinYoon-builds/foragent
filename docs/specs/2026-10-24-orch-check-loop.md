# D80: the orchestrator's check loop (skill rewrite)

Goal from Jin (2026-10-24): in a project folder, open Claude and type /fa; that window becomes the interviewer, the orchestrator and lanes run in the background, and the first screen shows status and Jin's decisions. This issue is the orchestrator's half of D80 step 2 in skills/fa-orchestrator/SKILL.md, built on the plan tools from #287.

- A check loop after every merge, every 30 minutes, and in the morning (day plan and briefing): goal progress, idle lanes, overlapping checks, quota, repeated failures.
- Hold the current goal and decide order and resources inside it with `fa orch queue`, `fa orch pause --why`, `fa orch resume`; record why. Keep blocked_by for real dependencies, never for ordering (on 2026-10-23 ordering links held 30 orch issues behind #138 for 8 hours).
- When failures repeat, follow the measure-first procedures in skills/fa-orchestrator/references/ (#274, D79).
- Does not duplicate #156 (ask only the five D71 kinds) or #226 (read the scorecard each cycle): link to them.
