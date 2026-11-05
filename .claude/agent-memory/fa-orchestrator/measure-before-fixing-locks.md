---
name: lock-deadlock-diagnosis
description: lsof open modes do not show who holds a flock; sample every thread and look for opposite lock orders (ABBA) before blaming one process
metadata:
  type: feedback
---

On 2026-11-05 17:00-22:02Z the whole gate froze. I blamed run-issue 2158 because lsof showed state.lock open twice (6w 7w) and asked Jin to kill it. Architect r821 showed that was wrong: lsof modes are open modes, each thread opens its own fd, and 2158 held nothing. The real cause was an ABBA deadlock: orch_facts._freeze held state.lock shared and waited on capacity.lock, while capacity.admit_run held capacity.lock and waited on state.lock. A kill of the tick (30394) released it.

**Why:** a wrong culprit costs a person's action and hides the real defect.

**How to apply:** for a flock freeze, `sample` every waiting pid and read which lock each thread waits on and which it already holds (main thread stack), then look for two processes taking the same two locks in opposite order. Name the cycle before naming a process to stop.
