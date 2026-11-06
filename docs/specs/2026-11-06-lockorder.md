# facts lock order (2026-11-06)

Architect r865. Reproduced 06:02Z and by the orchestrator:  raises RuntimeError Lock order inversion. The facts read takes state locks in one order and the lock guard checks another, so names where one store's name extends another's ('-', '.', before '/') fail. Callers that lose facts: fa orch facts, role wakes, stall signals, lane facts.
