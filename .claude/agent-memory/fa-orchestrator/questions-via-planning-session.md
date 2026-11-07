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

Update (2026-11-04, PM r406): plans inside a scope Jin already decided (including PM-requested bug and first-impression work within that scope) are approved by the orchestrator directly after a clean preread; do not wait for PM approval. Only Jin's share (taste, out-of-scope, hard to undo) goes to the box. Done that day for #1793.

**11-07 (PM r1009):** an order that sets scope for a decided redesign (one-path, Jin q57 A) is still approved first-hand by Jin through planning. I publish it unapproved and send the PM "ready for approval" with the test status; I approve only choice-free fixes myself.

**11-07:** a PM answer to a gate review-decision question (q60) does not by itself clear an "orchestrator-timeout" needs-human flag set while the question waited. After the answer is recorded, retry the issue with the decided rule as the hint; that clears the flag (#2458).

**11-07 #2501 note:** repeated_design_holds still lists #2209 #2215 #2312 #2454 #2458 until about 11-14, because repeat-check holds before 843f40f3 have no recorded key. Answer that gate signal not-now with this pointer; only a hold recorded after 843f40f3 that is still counted is a new defect.

**11-07:** once the PM takes over a draft plan (e.g. drafts/2026-11-07-defect-class rewritten by the PM), it has one writer: the PM. Do not edit it in parallel; a concurrent edit interrupted preparation (r1058). Report findings to the PM instead.
