---
name: project-hold-sources
description: fa brief --report (#756) hold/resolution rule — orch-events.jsonl only, never overwritten snapshots
metadata:
  type: project
---

#756 `brief_report._resolved_holds` reads holds and their ends only from the append-only `orch-events.jsonl` (spec update 2026-10-27).
- Hold kinds: escalate, retries-exceeded, stalled, orchestrator-timeout, parked. A hold may be older than the window.
- Resolving kinds: retry, resolve, unparked, or a gate merge (`presence.gate_merges`, the statusline's source). Only the end must be inside the window (`since < t <= now`); the latest end's say wins, else a default sentence (merge: "merged").
- A merged issue with a resolved hold appears once, under holds resolved. Report time `<state>/report.json`; missing/broken/future → last 24 h.

**Why:** Earlier attempts read `flags/*.json` and approval records. Reviewers rejected them twice: flags keep only the latest flag (they lose history), and approvals are saved before prerequisites (they are not proof of ready). Jin's spec then fixed the sources.
**How to apply:** Rebuild history only from append-only journals. Never read overwritten snapshots (flags, approvals, labels, runs/<n>.json state) to decide *when* something happened. When the spec lists event kinds, use exactly those.
