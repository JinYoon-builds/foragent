# Request box and question routing

Every request or question sent as a cross-session message is also written to the box, even when the
message itself arrives, so nothing is lost on restart.

Every question for the person, merge approval (`--kind merge`) and order card (`--kind order`) is
recorded with `fa orch ask`; AskUserQuestion is only for a person talking directly in the
orchestrator's own session. The planning session runs `fa plan heartbeat` and counts as alive for
120 seconds; its statusline keeps that heartbeat fresh while the session is open. If it is alive,
`fa orch ask` prints a planning handoff (id, kind, question, choices, recommendation; `--json` for
the same as JSON) without a push: send it to planning as a cross-session message with SendMessage.
If it is absent or its heartbeat is stale, `fa orch ask` sends the push itself, so do not add
`fa notify`. Act on the answer recorded by `fa orch answer ID --text "..."`, which closes only that
item and keeps its kind.
