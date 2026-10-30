# Order voice, skeleton contracts, planning handoff and stalled-request visibility

## Why — 2026-10-30

The person said: "무슨 소리를 하는지 전혀 모르겠군... 맥락이 하나도 없어서".
The order skill exposed internal decision-ledger lists, source labels and chunk cells without
explaining the person's problem or the result they would get. It also sent every preread unknown
back to the person, including details the session could settle from evidence or reverse later.

A finished lane-view plan then sat in the orchestrator's drafts for three hours. Its
request stayed processing, so the planning statusline needed to show the recorded wait. The
first proposal instead filtered out non-planning sources and hid the fact while any question
was open. That would leave the reported gate request r14 (since 10-26) and architect request
r28 (since 10-29) invisible too. The orchestrator skill's unconditional publish-after-[add] instruction conflicts
with approval being first-hand in the publishing session; relaying another session's answer
cannot authorize publication there. The same screen was split into eight interdependent pieces
without a common skeleton; preread found roughly eighty unknowns.

These are missing context, a contradictory handoff instruction, missing shared code contracts,
and missing visibility into recorded state. Keep judgment in the agents. The only runtime change
in this order reads literal box fields, compares timestamps, counts requests and formats a fact.

## Decisions

### A. Records stay records

Add `## Talking to the person` to fa-order. Record the decision ledger, chunk cells and source
labels with `fa order progress`. Do not show those records as lists unless the person asks.
Remove conflicting instructions to show the ledger, source labels, lane map or open-question
lists as the person's conversation. Keep the records and required progress commands intact.

### B. Context before a decision

Every question or confirmation starts with the problem in the person's own words, then what
will change for them when the work is done, then one decision to make, in that order. Preserve
the native interview, one question at a time, least-clear-cell selection and evidence sources.

### C. Quotes and summaries

Quote or attribute only words the person actually said, keeping their original words
untranslated. Mark a restatement of the agreement as the session's summary. Retain the existing
code/docs/person/assumption source labels in records.

### D. One scope confirmation

Confirm scope once with one question in the person's language; "Is this what you wanted?"
is an English example only. Reuse that confirmation while the scope still fits, and reconfirm
changed scope. Settle reversible details in the session and record them. Before approval, list
the decisions the session settled itself once in the restatement, as one short "I decided these
for you" list in the person's language, so the person can change them. The ledger, source labels
and chunk cells stay internal. Resolve preread unknowns using code, docs and settled decisions
before asking the person about genuine product or approval decisions, with context. Do not
weaken the clean-preread requirement.

### E. First-hand approval and item 2: planning publishes

Approval must come from the person in the session that publishes. Keep fa-orchestrator/SKILL.md
within 150 lines: replace its order-card paragraph with a one-line pointer; put the detailed
handoff in references/order-card.md. Keep required publication and [add]/[edit]/[later] cues
in that pointer for existing locked readers.

The plan JSON lives in `~/.local/share/foragent/<repo>/drafts/<order-id>/plan.json`, never /tmp
or a worktree. `<repo>` is StateStore's directory name (repo slug with `/` replaced by `__`).
Keep the originating request ID with the draft. For an orchestrator draft, put the actual state
drafts plan path, its current content hash and the originating request ID in the question string
passed to `fa orch ask --kind order --question`. Use `plan_hash` from preread/perspectives or
sha256 of the file, refreshing it after any plan edit. Send the recorded question, path and
printed handoff to planning using the existing cross-session channel. When the planning
heartbeat is stale or absent, `fa orch ask` already pushes the question to the person (#970),
including the durable path and hash; a closed planning session must not hide the plan.

The planning session asks the person, records their answer and runs
`fa order publish --approve PLAN.json` after first-hand approval there. After successful
publication of an orchestrator-drafted plan, that planning session marks the originating
request done with `fa orch request-status ID done --note "<issues>"`, using the request ID,
not the question ID, and the published issue numbers. Failed publication leaves it processing.
This closes the actual request instead of teaching a statusline to guess whether work is done.
An answer relayed to the orchestrator records the choice; it does not authorize publication.

The orchestrator may publish with `--approve` only if the person approved in its own session.
Retain the direct-session AskUserQuestion path, [add]/[edit]/[later], dry-run and clean preread,
partial approval and re-approval after scope edits. Apply the same approval boundary to tidy
orders. No new ask argument, transport or question schema is needed. This workspace's artifacts
are for review and validation; this run does not publish or create operational state drafts.

### Item 1. Skeleton first for real shared code dependencies

In fa-order step 4, when several issues depend on each other's code (shared interfaces, or one
screen split into parts), make the first issue a skeleton that defines shared names, signatures
and data shapes in code. List those contracts under a Contracts heading in the order spec.
Every dependent issue names the skeleton issue's key in `blocked_by`. This is an agent's
code-informed decomposition decision; do not add a script that guesses dependencies or screen
meaning. It is not a blanket serialization rule for unrelated issues.

### Item 3. A local stalled-request fact in the planning statusline

Append the fact through the existing i18n `text()` function and presence message catalogue:
- en: `Stuck requests: N (over M h) · fa orch inbox`
- ko: `요청 N개가 M시간 넘게 멈춰 있어요 · fa orch inbox`

N counts every request in the box with exact `status` of `processing` whose last actual status
change is strictly older than 60 minutes relative to the public function's `now` argument.
Every source counts, including planning, gate and architect. Open human questions of any kind
never suppress this fact. Exactly sixty minutes and younger ages are excluded. M is whole
hours, rounded down, since the oldest qualifying request's last status change. The copy works
for N=1 and includes the next action. Other languages use the existing presence fallback via
`text()` (English for unsupported languages, normal language-region handling).

Only append the new fact in a planning session: the same condition as `renew_planning`, with
no `FORAGENT_ROLE` key at all in the environment (even an empty value excludes it), and cwd
outside the state's `worktrees` folder, resolving paths so nested folders and symlinks count.
Lane and orchestrator sessions retain the existing counts but never see this fact. Share the
predicate with heartbeat renewal so these session boundaries cannot drift.

`set_request_status` records same-status notes and updates `updated_at` too. Read the ordered
notes' literal status transitions, ignoring consecutive repetitions of the same status; a new
request starts in status `new`. A real transition back into processing starts a new age. For
legacy requests with no status change recorded, use `created_at`; never use `updated_at` as
an age substitute. A malformed or timezone-less selected timestamp skips just that request;
an invalid recorded transition must not fall back to creation. Other qualifying requests and
all existing statusline counts still appear, even if every request timestamp is invalid.
Keep the existing statusline setting, no-project/no-store behavior and local-only behavior.
The feature adds no subprocess, network request, heartbeat, notification, retry or auto-recovery.

## Contracts

The three implementation issues in this order change disjoint production files and need no
new shared skeleton, so all `blocked_by` arrays are empty. The skeleton-first rule applies to
future orders whose children actually share code contracts.

Existing contracts consumed by the presence issue (no changes to orch_inbox needed):
- `presence.statusline_segment(cwd: Path, now: datetime) -> str | None` stays the public entrypoint.
- `orch_inbox.requests(store, pending=True)` exposes each row's `from`, `status` and ordered
  `notes` entries with literal `status` and ISO `at` timestamps, plus `created_at` for legacy rows.
- Open questions do not filter stalled requests or their ages.
- `i18n.text(key, language, **values)` formats the localized fact in messages/presence.py.
Any helper inside presence.py is an implementation detail, not a new cross-lane API.

## Lane ownership and verification

Use `.foragent.yml` as exported with this plan. Both order and design list skills/fa-order/**;
choose order because this changes order preparation, not visual direction. The orchestrator
skill and order-card reference belong to orch. Presence and its feature message catalogue are
unowned, so use the implicit main lane. Add no lane or config change.

The three issues carry four complete locked acceptance files. Keep every existing test function
name, updating the old source-filter and open-question tests to assert the corrected behavior.
Each new collected case must fail on exported main and pass with the proposed replacements.
Keep all specified order regressions and every discovered orchestrator-skill reader passing,
plus heartbeat/session and existing #970 routing checks. For presence exercise the public
function with a real temp store; cover all sources, open question kinds, role/env/worktree
boundaries, malformed and naive transition/creation timestamps, nonzero base counts, legacy
creation fallback, repeated notes, oldest age, strict threshold and language fallback.

Break-it checks must catch wrong count, missing/inclusive thresholds, open-question suppression,
planning-source filtering, role/worktree leaks, ignored legacy creation, timestamps hiding valid
requests/base counts and repeated notes resetting age. Also mutate the skill language, original
quotes, self-settled decisions, request completion, durable plan path/hash and stale-planning
push guidance. These bounded documentation checks are contracts, not runtime classifiers.

## Out of scope

No publication, approval, commit, push, checkout, reset or stash during this preparation. No
edits to the shared source checkout. No CLI approval bypass, approval identity redesign, inbox
schema migration or second writer for request state. No semantic keyword lists, similarity
thresholds, LLM calls in scripts or dependency guessing. No redesign or implementation of the
previous lane-view screen, retrospective policy, scheduler retries, notifications or unrelated
skills. Do not loosen existing locked tests or the clean-preread gate.

## Proposed skill edits

The following diffs are self-contained reference wording. Implement equivalent focused changes
while satisfying the locked behavior checks and existing tests. Presence behavior is specified
above and tested through its public function; no helper name or internal layout is prescribed.

```diff
--- a/skills/fa-order/SKILL.md
+++ b/skills/fa-order/SKILL.md
@@ -18,6 +18,28 @@
 conversational English, active voice, positive phrasing, and one idea per sentence.
 Explain the result and the next action in the person's terms. Run the commands in this
 skill yourself; the person supplies requests, links and choices in conversation.
+
+## Talking to the person
+Record the decision ledger, chunk cells and source labels with `fa order progress`.
+These records are not shown to the person as lists unless the person asks for them.
+
+Every question or confirmation to the person starts with this context, in order:
+the problem in the person's own words (what they asked or worried about), then what
+will change for the person when the work is done, then one decision to make.
+
+Only words the person actually said go in quotation marks or are attributed to them.
+Keep the person's original words untranslated in quotes.
+Mark a summary of the agreement as the session's summary.
+
+Confirm scope with one question, e.g. "Is this what you wanted?" in English;
+ask the confirmation question in the person's language. The session settles
+reversible details itself and keeps them in the records. Reuse that confirmation
+while the scope still fits; ask again only when the scope changes.
+
+Approval must come first-hand from the person in the session that publishes. The planning
+session publishes only after the person approves there. When the orchestrator drafts the
+plan, it sends the plan path to the planning session. Approval relayed from another session
+is not approval.
 
 ## Local preparation progress
 At order start, immediately run `fa order progress start --title "<request summary>"`,
@@ -55,10 +77,10 @@
   date (say unknown when the source has no date). Label each source: `code`, `docs`,
   `person` or `assumption`; cite a file/section or the person's words. Assumptions stay
   provisional until resolved. Record settled entries with `fa order progress ledger`.
-- Show the collected ledger once for confirmation, as one concise list. Correct it from
-  the person's response; then ask only about gaps. If this session already confirmed
-  the ledger, reuse it and show only changes. Documents fill gaps; they do not constrain
-  what the person may request now.
+- Once, confirm the person's goal in context as described in Talking to the person.
+  Correct it from the person's response; then ask only about gaps. If this session already
+  confirmed the goal, reuse it and explain only scope changes in context. Documents fill
+  gaps; they do not constrain what the person may request now.
 - Existing open issues without the Foragent format: propose per issue — rewrite (contract +
   acceptance test), split, merge duplicates, close (with reason, original kept) or leave
   (not finished by code). Apply only what the user approves; never delete.
@@ -101,9 +123,10 @@
 
 For the native interview, use AskUserQuestion, **one question at a time**. Offer 2–3
 options with your recommendation first and marked, while accepting a free answer.
-First show the big picture as proposed chunks and confirm that it matches the request.
-Then work on the least clear remaining cell, using the ledger and code to fill anything
-you can answer yourself. Show its source labels and the evidence behind the options.
+Reuse the goal confirmation from step 1; if it is still missing, ask the single scope
+question described in Talking to the person. Then work on the least clear remaining cell,
+using the ledger and code to fill anything you can answer yourself. Keep source labels in
+the records; explain the practical tradeoffs and evidence behind options in context.
 Refine a free answer into decision / reason / constraints / out of scope, and reflect it
 back for correction before treating an uncertain interpretation as settled.
 Ask only what changes product behaviour, scope, priority, UX or an approval boundary.
@@ -111,9 +134,10 @@
 - New project (no specs yet): find the goal first — who uses it, the first thing they do,
   what "done for now" means, what is explicitly not in this round.
 - Daily order: process decisions/feedback, then the new request. Keep it short.
-Keep a visible list of **open questions** in your messages; the interview ends when it is
-empty and all five cells per chunk are settled. Stop asking once remaining choices are
-reversible; decide those yourself and record the assumptions.
+Keep **open questions** in the progress records; bring the next unresolved decision to
+the person with context. The interview ends when no open questions remain and all five
+cells per chunk are settled. Stop asking once remaining choices are reversible; decide
+those yourself and record the assumptions.
 
 ### Project profile
 Use `profile` from the context and the ledger as earlier answers, not a separate team
@@ -195,10 +219,13 @@
 visual Done when evidence so implementing workers can reproduce the agreed result.
 
 ## 3. Restate and confirm
-Before splitting, restate in a few lines: what will exist when this order is done, what is
-out of scope, and every decision you made on the user's behalf. Include the seed when
-Ouroboros produced one. Reuse the big-picture confirmation from step 2 if it still fits;
-otherwise get an explicit yes on the changed scope.
+Before splitting, give the session's summary in a few lines: the person's problem, what
+will change for them, and what is out of scope. Before approval, list the decisions the
+session settled itself once in this restatement, under "I decided these for you" (in the
+person's language), as one short list so the person can change them. Keep the decision
+ledger, source labels and chunk cells internal; this is a plain-language summary of choices.
+Include the seed when Ouroboros produced one. Reuse the goal confirmation from steps 1–2
+if it still fits; otherwise ask the single scope question on the changed result, in context.
 
 ## 4. Split into issues
 One issue = one PR that can be verified on its own, sized for one agent session.
@@ -206,6 +233,10 @@
 verification environment, an approval boundary, or a part that can be blocked while the
 rest still completes. For each issue you must be able to write one sentence:
 "Close when <specific evidence>". If you cannot, split or ask.
+When several issues depend on each other's code (shared interfaces, or one screen
+split into parts), the first issue is a skeleton that defines the shared names,
+signatures and data shapes in code. The order spec lists them under a Contracts
+heading. Every dependent issue lists the skeleton issue in `blocked_by`.
 - **Lane** = the area of the code it changes. Issues in one lane run one after another;
   lanes run in parallel. Use existing lanes; propose a new lane only for a genuinely new
   area (name, paths, confirmed fixed style skills from `fa skills catalog`). Never put two
@@ -226,12 +257,12 @@
   when the affected files are not yet known. The published issue lists these under `## Files`.
 - Put the shared outcome and cross-issue constraints in the parent summary, not in children.
 
-Show the lane map before writing acceptance tests: chunk × goal / out of scope /
+Record the lane map before writing acceptance tests: chunk × goal / out of scope /
 Done when / lane / skills, with source labels for the settled facts and open cells.
 Fill lanes from code paths and the existing lane definitions; ask only when ownership is
-unclear or a new lane needs agreement. Confirm the map once all five cells are settled,
-including executable check commands and the skills policy above. Keep reporting the
-corresponding `fa order progress` facts; unanswered cells stay open.
+unclear or a new lane needs agreement. Settle all five cells, including executable check
+commands and the skills policy above. Keep reporting the corresponding `fa order progress`
+facts; unanswered cells stay open. Reuse the person's scope confirmation from steps 1–3.
 
 ## 5. Write the final checks before any code exists
 For every issue, write the acceptance test file(s) now. They are the goal; the implementer
@@ -251,7 +282,8 @@
 
 ## 5b. Challenge the spec from other seats
 Once the spec is roughly formed, before you lock it for approval, write the plan JSON
-(schema in `fa order publish --help`) to a temp file and run
+(schema in `fa order publish --help`) to the state drafts folder
+`~/.local/share/foragent/<repo>/drafts/<order-id>/plan.json`, never /tmp or a worktree, and run
 `fa order perspectives PLAN.json --json`. Read-only agents question it in parallel from
 fixed seats: skeptic, first-time user, attacker, operator, maintainer in six months,
 incidents (risk map and decisions), and other users. A failed seat is listed under
@@ -263,25 +295,22 @@
 shows which seats changed specs.
 
 ## 6. One approval
-Write the plan JSON (schema in `fa order publish --help`) to a temp file. Run
+Keep the plan JSON in that state drafts folder (schema in `fa order publish --help`). Run
 `fa order publish PLAN.json --dry-run`, fix validation errors, then record `progress check`.
 Then run `fa order preread PLAN.json --json`. The implementer reads each issue read-only and
-lists what it would have to guess. Show every unknown to the user as it is and ask them to
-answer it; never answer an unknown yourself. Fold the user's answers into the plan, then run
-preread again until every issue has no unknowns.
+lists what it would have to guess. Resolve unknowns from code, docs or settled decisions,
+and settle reversible details yourself, recording them in the plan. Bring only unknowns
+that need the person's decision to them, one at a time with context. Fold the answers into
+the plan, then run preread again until every issue has no unknowns.
 `--approve` refuses issues whose latest preread of the current content is missing or has
 unknowns; any plan edit needs a new preread. The dry-run is never blocked.
 Issues on a lane this order creates (`new_lanes`) have no implementer until the lane exists,
-so preread refuses them: publish once without `--approve` to create the lanes, run preread,
-then publish again with `--approve`.
-Show the order grouped by lane, work first:
-```
-This order: 7 issues
- api/  (lane api)   login API → token refresh → payment API     [m, m, l]
- web/  (lane web)   login page → error message → payment page   [m, s, m]
- docs/              README                                      [s]
- runs in parallel: api, web · skills: api=testing-strategy
-```
+so preread refuses them: after the person's first-hand approval here, publish once without
+`--approve` to create the lanes, run preread, then publish again with `--approve`. Reuse that
+approval only while the scope is unchanged; get approval here for any changed scope.
+Present the session's summary of the person's problem, the result they will get and
+what is out of scope. Explain any effect of lane assignments on delivery in their terms;
+keep the lane map and implementation details in the progress records.
 Before asking for approval, check every issue and fix the plan where a check fails:
 - (a) Reuse: does existing code already do this or most of it? If so, why is reusing it
   not enough? Name the measured gap.
@@ -297,14 +326,19 @@
 the dry-run and present the revised scope for approval.
 
 ## 7. Publish
-Run the validated `fa order publish PLAN.json` for real with
+After receiving the person's first-hand approval here, this planning session runs the
+validated `fa order publish PLAN.json` for real with
 `--approve` (or `--approve-keys` for a partial approval). `fa order publish` places the
 acceptance files and the spec in the checkout itself: it takes a publish lock, fetches and
 fast-forwards to the latest base branch, writes and commits only this order's own paths,
 and pushes. Other sessions' edits stay out of the commit, so no manual rebase is needed. If
 it cannot fast-forward, report the error instead of rebasing or committing by hand. Decisions and feedback that change
 existing issues go into `edits`. Record `progress publish --issue <number>` for each
-published or updated issue in the result. Show the printed summary and one line on what
+published or updated issue in the result. After this planning session successfully publishes
+a plan the orchestrator drafted, mark the originating request done with
+`fa orch request-status ID done --note "<issues>"`, using the originating request ID and the
+published issue numbers. A failed publication leaves the request processing.
+Show the printed summary and one line on what
 happens next ("the gate starts within 5 minutes; /fa shows results"), then run
 `fa order progress end`.
 
```

```diff
--- a/skills/fa-orchestrator/SKILL.md
+++ b/skills/fa-orchestrator/SKILL.md
@@ -129,10 +129,7 @@
 | Thought | Record it in the project's decision notes only; it creates no issues or approval. |
 | Setting change | Run `fa settings set KEY VALUE` for a key `fa settings list` shows ("turn off automatic merge" means `review batch`), with no extra confirmation, and report the change to the person in one line; for a team-scope key say it applies to the whole team, then commit and push `.foragent.yml` from a temporary worktree of the default branch. Unknown key or rejected value: one line with the valid choices. Details: [settings](references/settings.md). |
 
-Keep one order draft in `docs/specs/`; split it with the `fa-order` skill. When clear, propose one card with new
-issues by lane, dependencies and Done when checks. Ask (`--kind order`): [add] (recommended) / [edit] / [later]. After
-the user chose [add], publish with `fa order publish --approve PLAN.json`. Never add `approved` except through
-`fa order publish` after the user chose it. Card details: [order card](references/order-card.md).
+For order drafts, use `fa-order`; follow [order card](references/order-card.md) for [add]/[edit]/[later], the planning handoff and first-hand approval before `fa order publish --approve`.
 
 ## Design handoff from planning
 
@@ -147,4 +144,4 @@
 with the evidence numbers in `fa orch inbox` per due period; it never orders tidy work. Judge whether
 to call the architect (`fa orch architect --why "<numbers>"`), which reads widely, ranks and proposes
 refactors ([checklist](references/tidy.md)). Propose the tidy order with `fa orch ask`; after the user
-approves, publish it via `fa order publish`, each issue in the lane that owns its files; Done when: all existing tests still pass, behaviour unchanged. Then `fa tidy done`.
+approves in the publishing session, follow [order card](references/order-card.md), each issue in the lane that owns its files; Done when: all existing tests still pass, behaviour unchanged. Then `fa tidy done`.
```

```diff
--- a/skills/fa-orchestrator/references/order-card.md
+++ b/skills/fa-orchestrator/references/order-card.md
@@ -1,20 +1,42 @@
 # Order card
 
-Do not turn every sentence into a new issue. Keep one durable order draft in `docs/specs/` and use
+Do not turn every sentence into a new issue. Keep one durable order draft in the state drafts folder and use
 the project's decision log for settled context. Use the repository's `fa-order` skill to clarify
 only unresolved product choices, split work into independently verifiable issues and prepare a plan
 JSON. When clear, propose one card with new issues grouped by lane, dependencies, Done when checks,
 and any new lanes (human name, paths, skills).
 
-Record the card's choice with
-`fa orch ask --kind order --question "<card summary>" --option add --option edit --option later --recommend 1`.
-While the person is talking directly in this session, ask with AskUserQuestion; otherwise send the
-planning handoff that `fa orch ask` prints to planning with SendMessage. Act on the person's choice
-recorded by `fa orch answer ID --text "<choice>"`; publish only after [add].
+Store the plan JSON in `~/.local/share/foragent/<repo>/drafts/<order-id>/plan.json`,
+never /tmp or a worktree. `<repo>` is the state directory name (repo slug with `/` replaced
+by `__`); use this actual path wherever PLAN.json appears below.
+Keep the originating request ID with the draft so the publishing session can close that request.
+Use the current content's `plan_hash` from preread/perspectives, or compute the file's sha256;
+refresh the hash after any plan edit.
+
+When the orchestrator drafts a plan, write the plan JSON path and content hash into the order question:
+`fa orch ask --kind order --question "<card summary>; plan JSON path: <actual state drafts path>; plan_hash: <content hash>; originating request: ID" --option add --option edit --option later --recommend 1`.
+Use the handoff printed by `fa orch ask`. Send this question and plan path to the planning session
+with SendMessage. That session asks the person and, only after approval there, publishes with
+`fa order publish --approve PLAN.json`. The planning session records the answer with
+`fa orch answer ID --text "<choice>"`; a relayed [add] alone does not authorize publication.
+When the planning heartbeat is stale, `fa orch ask` already pushes the question to the person
+(#970), including its durable plan path and content hash, so a closed planning session does
+not hide the plan. Keep that existing route; no new notification mechanism is needed.
+
+After the planning session successfully publishes the orchestrator-drafted plan, it marks
+the originating request done with `fa orch request-status ID done --note "<issues>"`, using
+the originating request ID (not the question ID) and the published issue numbers.
+A failed publication leaves the request processing.
+
+The orchestrator publishes with `--approve` only when the person approved in the orchestrator's own session.
+In that case ask with AskUserQuestion, record the answer, and publish after [add].
+Approval relayed from another session is not approval.
 
 [edit] revises the draft; [later] keeps it for later without publication. Validate with
 `fa order publish PLAN.json --dry-run` before publishing (use selective approval flags if
 requested). Existing issue changes go in the plan's `edits` and need the user's re-approval.
+Use a clean preread of the current plan before `--approve`; scope edits need re-approval
+in the publishing session. Never add `approved` yourself outside `fa order publish`.
 Summarize what was recorded or published and resume watching without asking the user to prompt you
 again. Retain compact pending decisions, attempts and draft references across turns; re-read
 current facts when handling the next event.
```



## Issue 4 addendum: approved design references travel with the order

### Why — Jin, 2026-10-30

The approved lane-view prototype existed only in a scratch folder. Publication gathers
only the spec and acceptance files; preread sends the spec inline and reads the current
repository, which does not yet contain those artifacts. Implementers lacked the approved
pixels and copy and asked roughly forty questions. The design-session skill also sends
its handoff back through the orchestrator even though it already runs inside planning.
The root cause is missing artifact transport and an unnecessary relay instruction.

This addendum supersedes the earlier statement that presence is the only runtime change.
The original three issues, tests and reference proposals remain unchanged. Their production
files remain disjoint from each other; issue 4 overlaps issue 1 in fa-order/SKILL.md only,
so issue 4 is blocked_by order-voice-and-skeleton. order.py is owned by both order and board
in .foragent.yml; choose order because this change extends order authoring and publication.

### Transport contract

Plan JSON optionally has reference_files, an object keyed by normalized repository-relative
file paths under docs/design/**. Text values are strings (including empty strings). Binary
png/jpg/gif values are objects with exactly path and base64; path must equal the map key.
Example: {"docs/design/lane-view/approved.png": {"path":
"docs/design/lane-view/approved.png", "base64": "<encoded image bytes>"}}.
No .b64 sidecars. Reject absolute paths, .., nonnormalized paths, symlinks and paths outside
docs/design/. Decode strict base64, accept only the listed image extensions for binary
objects, and reject path/key mismatches or malformed object shapes before any writes.
Each file has a 1048576-byte (1 MiB) cap, measured as UTF-8 text bytes or decoded image
bytes; exactly the limit is allowed. Errors name the file and the cap or path constraint.
The deterministic schema checks do not infer visual meaning or issue dependencies.

validate_plan returns spec/test/reference contents as str or decoded bytes. _publish_files
writes and compares bytes and feeds the same complete path set into the existing single
order commit and push path. Dry-run includes every reference path and writes nothing.
Keep approval, clean preread, retries and existing no-reference plans compatible. Reference
blob changes invalidate approvals of issues relying on them, like acceptance-file changes.
Document the schema and limit in fa order publish --help.

### Reference discovery and preread

New issues may list references: [paths]; every path must be a key in reference_files. For
simplicity, omit references to include all plan reference files; [] explicitly selects none.
Use the same optional field for edits. Published issue bodies have a ## References section
with repository links for the selected files, separate from ## Files (implementation edits).

Preread currently receives spec_markdown inline through preread_prompt/preread_target and
Executor.run(read_only=True, worktree=root). Give it selected reference text inline the
same way. Before starting the reader, materialize decoded reference files into the ignored,
content-addressed root/.foragent/preread-references/<references-hash>/docs/design/... tree.
Include selected image files' absolute paths in the prompt, directly usable by an image
viewer. Preserve any existing design files; do not write to their publication destinations
before publish. Keep artifacts readable for the reader's lifetime, and keep reference bytes
in the existing preread content hash so changes require fresh clean evidence. No model or
keyword classifier in scripts; the agent decides which approved artifacts an issue needs.

### Skill correction

In fa-order's Design first section, put approved drafts, runnable prototype sources,
HANDOFF.md and screenshots in reference_files under docs/design/<topic>/; visual issues
point to them with references. In fa-design-session, continue with fa-order in this same
planning session with those artifacts. Remove the fa orch request handoff command and its
relay prose, including the stale description. Keep third-party research images local;
approved screenshots of the chosen draft travel with the order. Preserve existing design
interview, board, running-draft, preview-cleanup and first-hand approval behavior.

### Locked evidence and break-it checks

Add tests/acceptance/test_order_design_references.py and
tests/acceptance/test_fa_order_design_references.py with complete contents below in issue 4.
Exercise the real publish CLI parser/handler with the existing offline tracker/remote seams,
order.publish with the existing fake tracker and a byte-preserving PublishingGit variant,
and preread input construction plus its read-only executor invocation. No real Git commit
or push is run, including inside tests. Check same-commit bytes, idempotent retries, text
and png/jpg/gif, CLI dry-run names and no writes, invalid namespace/traversal/absolute and
symlink paths, strict base64/object/selection validation, multibyte and decoded size caps,
exactly-limit acceptance, issue links and explicit/default/empty selections, edit revision
handling, approval invalidation and fresh text plus actual viewable image files in preread.
Skill tests use casefolded whitespace and bounded regexes tying each instruction to its
object, and reject the old relay. All new cases fail on main and pass with the proposal.

Extend verify.py without changing the first three issues' checks. Run every discovered
existing test_order*.py, test_design_session*.py and test_fa_order*.py in tests/ and
acceptance/, plus all previous regressions. Mutate reference publication, namespace, size
cap, binary decoding, dry-run listing, body links, preread text/image access, approval
invalidation, help schema and both skill instructions; every mutation must fail a new
locked assertion without collection errors. Validate this four-issue plan and run a real
CLI schema dry-run with fake tracker/remote seams because repo/ is a Git archive.

The complete issue-4 reference implementation is in proposed/src/foragent/{order.py,
order_files.py,commands/order.py} and proposed/skills/{fa-order,fa-design-session}/SKILL.md
in this review workspace. The fa-order proposal layers only the Design first addition on
SKILL.proposed.md, preserving issue 1's original reference artifact. Publication of this
order commits only its spec and locked tests; these proposed production replacements are
for review and implementation, not production files to publish during preparation.


Compatibility note for issue 4: the existing locked design-session reader literally
requires the strings orchestrator, fa orch request and lane issues. Preserve that reader
without retaining the relay command: say that planning splits the lane issues and explicitly
prohibit relaying the handoff through the orchestrator with fa orch request. Remove the old
runnable --from planning handoff line. The new bounded check requires the prohibition and
rejects the former command; this is a workflow correction, not an added relay option.
Reference paths also remain single-line, consistent with existing issue file path contracts,
so reference links cannot introduce Markdown sections or commands.
