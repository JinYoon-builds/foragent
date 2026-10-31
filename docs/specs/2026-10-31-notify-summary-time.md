# Notification retry regression: deterministic time

The defect is in tests/test_cli.py, not notification scheduling. The retry test's
off-summary scenario must not depend on the ambient wall-clock hour. Keep its
exact item output, delivery-error logging, retry after failure, and deduplication
after success. It must remain green at both 2026-10-31T07:57Z and 08:06Z under UTC
and Asia/Seoul, including the currently failing ambient hours 08 UTC / 17 Seoul.

Preserve the public `fa inspect notify` contract in docs/inspect.md: new human
questions notify once; a failed delivery remains retryable; the daily summary is
due only during the configured local hour, once successfully delivered per local
date. The default is 8; configured hours replace it, including midnight. Item and
summary deduplication survive reopening state. Notification errors remain visible.

One size-s issue, owned by implicit main because no .foragent.yml lane path
matches tests/test_cli.py. Unit tests belong to their file's lane; ownership of
src/foragent/cli.py does not transfer ownership of this unit-test file.

The locked acceptance runs the existing regression under fixed ambient clocks
through the real public CLI and separately preserves documented CLI behavior.
Its red cases on main identify the faulty test; its already-green behavior cases
prevent suppressing legitimate summaries to make that faulty test pass.

Out of scope: production notification changes, changed summary semantics,
weaker or skipped regression checks, lane changes, and unrelated test cleanup.
Implementation details of the unit-test correction belong to the lane.
