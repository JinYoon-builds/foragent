# #413 heartbeat on gate launch (2026-10-30)

Second SPEC-CONFLICT of the same class: tests/acceptance/test_orch_spawn_liveness.py:199 required the heartbeat bytes to stay unchanged after a gate spawn, while #413's Goal requires normal gate launches to write the new session descriptor into the heartbeat at once. The Goal is the decided behaviour. The test now requires the heartbeat to change and to name the spawned session's --session-id. Other locked spawn tests were checked: line 119 covers a liveness check without a spawn and stays as is.
