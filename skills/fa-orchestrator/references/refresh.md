# Planned refresh (#413)

A long session loses detail at each compaction. Replace it with a fresh one on purpose.

1. On each check, run `fa orch refresh-due --json`. It reports facts only: this session's
   `compactions`, `age_seconds`, the `thresholds` (`orch_refresh_compactions`, `orch_refresh_hours`),
   `due` and a `reason`. Whether now is a good moment is your call.
2. When it is due and no person is talking with you, write a handoff note with these `##` sections,
   each filled or `none`: Current work; Watches; Open promises (with their triggers); Temporary
   settings (with revert conditions); Queued orders not yet published (with test locations);
   Pending decisions for the person; Peers; Recent incidents.
3. Store it with `fa orch handoff --file NOTE.md`. It refuses a missing or empty section.
4. Run `fa orch refresh --ready`. It refuses without a note stored after the last refresh, while a
   person's inbox request from the last five minutes is not done, or while a question is open or
   was answered in the last five minutes. Otherwise it opens a fresh session that reads the note
   first and makes it the active one.
5. When it prints that this session is retired, finish your turn and end this session yourself.
   Nothing signals it, and its heartbeat writes are ignored from then on. If the spawn fails, this
   session stays active and the note stays usable.
