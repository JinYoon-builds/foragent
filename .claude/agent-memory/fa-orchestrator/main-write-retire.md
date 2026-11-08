---
name: main-write-retire
description: How to clear a blocked main-write queue head with fa orch main-write retire (hash id, not sequence); order #2759 retrospective 11-08
metadata:
  type: project
---

`fa orch main-write retire ID --why ...` takes the request's hash `id` from `fa gate status --json` `main_writes[]`, not the `sequence` number status shows (11-08: `retire 106` said "No main-write request 106"; the 64-hex id worked).

**Why:** the queue is FIFO; one blocked head (seq 106, a lock-removal of a closed issue whose file changed on main) held 51 later writes for most of 11-08, including the shard_times refresh that a closed issue's lock needed.

**How to apply:** when `main_writes` shows the head `blocked` with an error that can never clear (file changed since, conflict with main), retire it at once with the hash id and a why; then check the next owner turn lands the following requests. Requests 107+ (role memory, merge-tree errors) may need the same. Order #2760 took ~8 h approval→merge: PR CI cancellations (q89/q90) and a 2 h owner check from the #2176 lock ([[prove-locks-can-pass]]).
