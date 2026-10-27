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
2. Writes the request `<state>/stops/N.json` (why, requested_at, its own PID and start time),
   under a claim lock, so two stop commands never both own it. Every process a scan finds is
   recorded in the request too.
3. Waits for a merge call the runner already made, then terminates the tree once through
   `scheduler._kill_run(outcome="stopped")`.
4. Waits up to 60 s until every process of the tree is gone (the recorded processes, the runner's
   and executor's process groups and every descendant any scan found), then up to 5 minutes for
   `worktree_lock(N)`.
5. Commits the worktree as a WIP naming the why on `foragent/issue-N` (locked acceptance files
   restored from their approved blob hashes) and pushes the branch (never forced) when it holds
   work origin lacks. With none, the event says `nothing to save`.
6. Writes the `stop` event, finishes the run as `stopped`, closes this issue's open Foragent PR
   and returns the issue to `ready`. It deletes the request last.

The kill can land inside a runner step that replaces the worktree (a rebuild, a replay restore,
undoing a reviewer's edits). Each such step first writes a checkpoint of the run's work (HEAD and
staged tree) to `<state>/runs/issue-N.checkpoint.json`; the stop saves from it instead of the
half-built worktree.

A runner that finds the request exits with `stopped` and writes nothing: every refusal (`flag`),
GitHub write, WIP save and record finish checks it first. While a request exists, the tick starts
no new run of N. If the runner merged its PR, or the issue closed, before the tree died, the stop
reports that, deletes the request and changes nothing else.

## When a step fails

The command prints the failed step and exits 1. A failure before the kill removes the request;
after the kill the request stays and the next gate tick finishes the stop (every step repeats
safely). A rejected push keeps the WIP commit locally; its SHA is in the message. A second
`fa orch stop N` takes over a request whose stop command died and keeps what it recorded.

The tick leaves N alone while the stop command lives, and starts, kills or crash-finishes nothing
for N while a request exists. If that command died before its kill (the runner itself still
lives), the tick deletes the request with one `stop-request-expired` event and normal
stall and runtime limits apply again. If the runner is dead, the tick ends what is left of its
tree and finishes the stop itself. A request for a run that finished otherwise, or is missing,
expires 10 minutes after `requested_at`.
