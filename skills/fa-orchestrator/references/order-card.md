# Order card

Do not turn every sentence into a new issue. Keep one durable order draft in the state drafts folder and use
the project's decision log for settled context. Use the repository's `fa-order` skill to clarify
only unresolved product choices, split work into independently verifiable issues and prepare a plan
JSON. When clear, propose one card with new issues grouped by lane, dependencies, Done when checks,
and any new lanes (human name, paths, skills).

Store the plan JSON in `~/.local/share/foragent/<repo>/drafts/<order-id>/plan.json`,
never /tmp or a worktree. `<repo>` is the state directory name (repo slug with `/` replaced
by `__`); use this actual path wherever PLAN.json appears below.
Keep the originating request ID with the draft so the publishing session can close that request.
Use the current content's `plan_hash` from preread/perspectives, or compute the file's sha256;
refresh the hash after any plan edit.

When the orchestrator drafts a plan, write the plan JSON path and content hash into the order question:
`fa orch ask --kind order --question "<card summary>; plan JSON path: <actual state drafts path>; plan_hash: <content hash>; originating request: ID" --option add --option edit --option later --recommend 1`.
This handoff is for D71 choices and real preread questions (see below). Use the handoff printed by `fa orch ask`. Send this question and plan path to the planning session
with SendMessage. That session asks the person and, only after approval there, publishes with
`fa order publish --approve PLAN.json`. The planning session records the answer with
`fa orch answer ID --text "<choice>"`; a relayed [add] alone does not authorize publication.
When the planning heartbeat is stale, `fa orch ask` already pushes the question to the person
(#970), including its durable plan path and content hash, so a closed planning session does
not hide the plan. Keep that existing route; no new notification mechanism is needed.

After the planning session successfully publishes the orchestrator-drafted plan, it marks
the originating request done with `fa orch request-status ID done --note "<issues>"`, using
the originating request ID (not the question ID) and the published issue numbers.
A failed publication leaves the request processing.

Under D71, a choice-free fix (its required behaviour is settled and nothing is left for the person
to choose) is one the orchestrator itself publishes and approves with `fa order publish PLAN.json --approve`,
only after a clean preread. A choice about behaviour, scope, screen wording or the approval boundary,
or a real preread QUESTION, takes the planning handoff above: the planning session asks the person.
A Foragent caller role alone never blocks a choice-free publication; the D71 judgment stays yours.
Approval relayed from another session is not approval. The same boundary applies to tidy orders.

[edit] revises the draft; [later] keeps it for later without publication. Validate with
`fa order publish PLAN.json --dry-run` before publishing (use selective approval flags if
requested). Existing issue changes go in the plan's `edits`; an edit that changes a D71 choice
needs the person's re-approval through the planning handoff. Use a clean preread of the current
plan before `--approve`. Never add `approved` yourself outside `fa order publish`.
Summarize what was recorded or published and resume watching without asking the user to prompt you
again. Retain compact pending decisions, attempts and draft references across turns; re-read
current facts when handling the next event.
