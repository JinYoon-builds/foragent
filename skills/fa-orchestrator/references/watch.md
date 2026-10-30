# Watch details

One-shot `fa orch watch --interval 5` prints one JSON line and exits on a change; calls share
`<state>/orch-watch.json`, so an unchanged state waits. Its fields are `blocked`, `needs_human`,
`merged` (sorted issue numbers), and `queue_empty` (no open approved/ready/running issues and no
active runs). A line `{"unknown": true, "error": ...}` means the state could not be read: treat it
as unknown, not empty. One-shot watch then exits non-zero; `--follow` keeps retrying; the next good
read always prints the real snapshot. Read fresh status after a wake-up; a snapshot is a signal to
inspect, not a report.

`fa orch watch --follow` also prints one JSON line per new inbox request
(`{"event": "request", "id", "from", "text"}`) and per newly answered question
(`{"event": "answer", "id", "kind", "answer"}`), so a running Monitor wakes without another prompt.
Requests and answers present when it started count as already seen; request progress notes and
unchanged polls stay quiet.
