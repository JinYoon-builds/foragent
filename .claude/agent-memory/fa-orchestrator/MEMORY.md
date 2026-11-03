# fa-orchestrator memory

Durable lessons of this project's orchestrator. Only the orchestrator session writes this file.
- [Drafts outside scratchpad](pending-drafts-not-in-scratchpad.md) — waiting test rewrites/plans go to a durable path; re-approval recipe
- [Pending after merge](pending-after-merge.md) — follow-ups waiting on #962, #186/#435 merges
- [Questions via planning session](questions-via-planning-session.md) — Jin talks only in planning; since 11-04 plans inside Jin-decided scope: orch approves itself
- [Check held reasons first](check-held-reasons-first.md) — queued lane: read fa orch facts held: (cap vs overlap) before blaming files
- [Peer messages in Korean](peer-messages-in-korean.md) — SendMessage to the PM in Korean, call it "PM"; Jin sees them
- [Flow goal (11-01)](flow-goal-2026-11-01.md) — metric: part-5 real-waste minutes (no 70% target); flow5.py; retro reports it
- [No prompting commands](no-prompting-commands.md) — unattended: avoid rm -rf on variable paths; a prompt calls Jin to the screen
- [No chained publish](no-chained-publish.md) — publish alone, check approved label, then unpark and retry; order edits need a fresh preread
- [edit_plan dropped lines](edit-plan-dropped-done-when.md) — order edits lost Done when commands and checklist until 11-01; compare before publishing
- [Prove locks can pass](prove-locks-can-pass.md) — run related locks and a scratch prototype before publishing; five stops of this class on 11-01
- [Read result before reporting](read-result-before-reporting.md) — never report an outcome in the same step as the command
- [rerun-review needs CODEX_HOME](rerun-review-needs-codex-home.md) — export the live account first; empty review plus "Error finding codex home" is this
- [Lane draft runs load](lane-draft-runs-load.md) — draft agents' pytest sits outside test slots; one file, nice 15, targets only
- [Merge owner before launch](merge-owner-before-launch.md) — Jin 11-03: PM builds it outside FA; orch lane paused, #1491 #1641 #1760 parked until it merges
- [Order after 11-03](order-after-11-03.md) — merge owner, then 2-3 days cleanup, then features; no new internal-mechanism features
- [Stray state writes](inspect-prepare-real-state.md) — fake inbox questions came from fa inspect prepare running Try it; trace by timestamp + find -newermt
- [gh project scope](gh-project-scope-new-machine.md) — new machine gh token lacked project; publish committed then failed; Jin runs gh auth refresh -s project
