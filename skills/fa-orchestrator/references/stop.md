# Stopping a running issue

`fa orch stop N --keep-work --why "..."` stops issue N's run, keeps its unfinished work and
returns the issue to `ready`, so the next run continues from `foragent/issue-N`. Both flags are
required. The why text is in the WIP commit (`[stopped: <why>]`), the `stop` event and a GitHub
comment.

## Who owns the run (D95)

While its process lives, the runner alone writes the run record, its worktree and its labels.
The stop command acts only after the runner's whole process tree is dead. In order, it:

1. Refuses without changes when N is not running, the run is a batch run (`foragent/batch-*`),
   another live `fa orch stop N` owns the stop, or a recorded PID fails the kill-safety rules.
2. Writes the request `<state>/stops/N.json` (why, requested_at, its own PID and start time).
3. Terminates the tree once through `scheduler._kill_run(outcome="stopped")`.
4. Waits up to 60 s for every process of the tree to die, then up to 5 minutes for
   `worktree_lock(N)`.
5. Commits the worktree as a WIP naming the why on `foragent/issue-N` and pushes it (never
   forced). With no changes, the event says `nothing to save`.
6. Writes the `stop` event, finishes the run as `stopped`, closes this issue's open Foragent PR
   and returns the issue to `ready`. It deletes the request last.

A runner that finds the request at a finalizer exits with `stopped` and writes nothing. If the
runner merged its PR, or the issue closed, before the tree died, the stop reports that, deletes
the request and changes nothing else.

## When a step fails

The command prints the failed step and exits 1. A failure before the kill removes the request;
after the kill the request stays and the next gate tick finishes the stop (every step repeats
safely). A rejected push keeps the WIP commit locally; its SHA is in the message. A second
`fa orch stop N` takes over a request whose stop command died.

The tick leaves N alone while the stop command lives. If that command died before its kill (the
runner still lives), the tick deletes the request with one `stop-request-expired` event and normal
stall and runtime limits apply again. A request for a run that finished otherwise, or is missing,
expires 10 minutes after `requested_at`.
