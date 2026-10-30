# #413 spawn argv (2026-10-30)

The reviewer found SPEC-CONFLICT: tests/acceptance/test_orch_spawn.py:160 required the exact argv `claude --bg -n <name> /fa-orchestrator`, while #413's Goal (fixed answers 2026-10-29) requires every launch, including normal gate launches, to pass `--session-id <uuid>`. The Goal is the decided behaviour, so the test now accepts exactly one `--session-id <valid uuid>` pair in that argv and nothing else.
