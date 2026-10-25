# Facts show baseline capture time against its limit

Evidence (2026-10-25): the acceptance baseline broke four times in two days (#327, #325, #376, then the fixed 900 s limit) while capture time grew 195 → 485 → 736 s unseen, until every capture timed out and the gate could not merge. #427 records duration_seconds per cache entry. D88: this measures waiting before it stops the gate.

- fa orch facts lists baseline builds (key, SHA, time, tests, duration, success or timeout) and the newest duration against the limit in force, flagging near_limit at 75% or three rising durations; one human line; read-only.
