# Request box and question routing

Every request or question sent as a cross-session message is also written to the box, even when the
message itself arrives, so nothing is lost on restart.

Every question for the person, merge approval (`--kind merge`) and order card (`--kind order`) is
recorded with `fa orch ask`; AskUserQuestion is only for a person talking directly in the
orchestrator's own session. The planning session runs `fa plan heartbeat` and counts as alive for
120 seconds; its statusline keeps that heartbeat fresh while the session is open. If it is alive,
`fa orch ask` prints a planning handoff (id, kind, question, choices, recommendation; `--json` for
the same as JSON) without a push: send it to planning as a cross-session message with SendMessage.
If it is absent or its heartbeat is stale, the question waits in the FA inbox (`fa orch questions`)
for the next planning session (PM), which asks the person; never push it to the person directly,
not even with `fa notify`. Act on the answer recorded by `fa orch answer ID --text - <<'EOF'`, which closes only that
item and keeps its kind.

The box is a place on the accumulation object. The gate files one PM alarm (`from: gate`, `to: pm`,
`kind: shelf-alarm`) when more than 20 requests are open or the oldest has stayed new or processing
for 24 hours, and when a planning note has waited at its boundary for 24 hours; `fa brief --json`
lists open alarms for the PM. A request with a line `Decision: person` becomes one multiple-choice
question in `fa orch questions`; at most five such questions are created a day, counting
`fa orch ask` questions that carry the line, and `fa orch ask` refuses a sixth. When fewer issues run than
`target_concurrency` (default 12), the gate files one `decision-drafting` request naming the oldest
decisions with no issue yet and leaves a skeleton draft in `<state>/drafts/decision-<id>/plan.json`;
write the order from it and publish it with `fa order`.
