# Watch output

One-shot `fa orch watch --interval 5` prints one JSON line and exits on a change; calls share
`<state>/orch-watch.json`, so an unchanged state waits. `--follow` keeps printing changes until it is
stopped.

Fields: `blocked`, `needs_human`, `merged` (sorted issue numbers), and `queue_empty` (no open
approved/ready/running issues and no active runs).

A line `{"unknown": true, "error": ...}` means the state could not be read: treat it as unknown, not
empty. One-shot watch then exits non-zero; `--follow` keeps retrying; the next good read always prints
the real snapshot.
