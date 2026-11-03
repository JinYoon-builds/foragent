---
name: questions-via-planning-session
description: Jin talks only in the planning session; never AskUserQuestion in the orchestrator tab; new orders go to the planning session as a plan path to publish
metadata:
  type: feedback
---

Jin (2026-10-30, r50 + planning-session message): he talks only in the planning session, never in the orchestrator's tab. Send every question, merge approval and order card to the planning session (SendMessage to it and `fa orch ask`); do not use AskUserQuestion in this tab.

For a new order: when its plan passes dry-run and preread, send the planning session the durable plan path (`~/.local/share/foragent/JinYoon-builds__foragent/drafts/...`). It asks Jin and runs `fa order publish --approve` itself. A pre-approval relayed by another session does not let this session publish a new order (the permission classifier denied it as an unrequested commit) — so don't try; hand over the path.

**Why:** 2026-10-30 Jin cancelled an AskUserQuestion in this tab; a relayed approval was refused by the classifier. Edits to existing issues (test-owner corrections, redesigns, my own re-approvals) still went through from this session.

**How to apply:** new issues → plan path to the planning session. Edits/re-approvals of already-ordered issues → do them here, report in one line. Related: [[pending-drafts-not-in-scratchpad]].

Also refused on a relayed request (2026-10-30): closing an issue (#924). Closing or deleting on another session's word goes back to that session to do.

Update (2026-10-31, D101 a in docs/decisions.md): choice-free bug fixes I find are published and approved by this session after a clean preread (done for #1237 without refusal). Orders carrying Jin's decisions or a choice still go to planning as a plan path.
