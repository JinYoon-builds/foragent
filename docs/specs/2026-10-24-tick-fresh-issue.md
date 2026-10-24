# A tick acts on fresh issue state, not the list it read at its start

Evidence (2026-10-23, #163): GitHub shows `labeled blocked 16:34:18`, `unlabeled running 16:34:20` when the runner ended the run as `timeout`; the local snapshot recorded it at once (`RunContext` wraps the tracker in `RecordingTracker`, runner.py:77). Yet the tick that recovered #163 at 16:35:02 (and started #184 at 16:36:17) still saw `running`: a tick reads its issue list once at the start (scheduler.py:671) and uses it for minutes. `_recover_orphan` re-reads the run record (finished/timeout) but keeps the stale issue (`running`), so it writes a second `crashed` row (:274) and `_restore_after_crash` adds `ready` next to `blocked` (:300). The same staleness escalated a just-retried #178 to needs-human on 2026-10-23 morning.

- Before acting on an issue (orphan recovery, crash restore, escalation, starting a run), a tick re-reads that issue from the local snapshot and acts only if the condition still holds.
- A run that ended during a tick stays `blocked` with one history row; later ticks keep it for the orchestrator.
- A genuinely dead runner is still recovered once.
