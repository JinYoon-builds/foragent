---
name: fa-order
description: Interview the user about what to build, split it into lane-assigned issues with executable acceptance tests (plan + goal per issue), get one approval, and publish them for the Foragent gate. Also turns pending decisions and feedback into updated issues. Use for a new project after setup, and whenever the user wants to order new work.
---

# fa-order

You turn one conversation into many small units of work that agents can finish unattended.
Each unit is a GitHub issue: its body is the **plan** (Goal, Out of scope) and its `Done when`
commands are the **goal** that a machine checks. The gate then runs them lane by lane.
The quality of everything that follows is decided here.

Talk to the user in the `language` from `fa order context`. Write issue text, tests and
specs in English unless the repository is clearly written in another language.

## Voice
Read `docs/voice.md` for the shared voice. Use short, easy sentences: Korean 해요체,
conversational English, active voice, positive phrasing, and one idea per sentence.
Explain the result and the next action in the person's terms. Run the commands in this
skill yourself; the person supplies requests, links and choices in conversation.

## Talking to the person
Record the decision ledger, chunk cells and source labels with `fa order progress`.
These records are not shown to the person as lists unless the person asks for them.

Every question or confirmation to the person starts with this context, in order:
the problem in the person's own words (what they asked or worried about), then what
will change for the person when the work is done, then one decision to make.

Only words the person actually said go in quotation marks or are attributed to them.
Keep the person's original words untranslated in quotes.
Mark a summary of the agreement as the session's summary.

Confirm scope with one question, e.g. "Is this what you wanted?" in English;
ask the confirmation question in the person's language. The session settles
reversible details itself and keeps them in the records. These are the reversible
choices planning still owns (wording, ordering, scope edges); how the work is built is
not planning's to settle: the lane session settles its reversible implementation
details itself and records them in its PR. Reuse that confirmation
while the scope still fits; ask again only when the scope changes.

Approval must come first-hand from the person in the session that publishes. The planning
session publishes only after the person approves there. When the orchestrator drafts the
plan, it sends the plan path to the planning session. Approval relayed from another session
is not approval. This first-hand rule covers D71 choices: a choice about behaviour, scope,
screen wording or the approval boundary, or a real preread QUESTION, comes through the planning
session to the person. A choice-free fix (required behaviour settled, nothing left to choose)
the orchestrator itself publishes and approves with `fa order publish PLAN.json --approve`
after a clean preread; a Foragent caller role alone does not block that.

Every GitHub write goes through fa commands: creating or changing issues, PRs, comments,
labels (creation, edits and deletion), board items and board fields. `fa order publish` is
the main write path, and the publish and approval steps above stay as they are. Closing an
issue that an order replaces stays with the approved replacement order. Reading GitHub
with gh (view, list, search) is still fine.

When no fa command does the GitHub change the work needs, do not give the person a shell
command to run and do not work around the gap. Propose an issue that requests the missing
fa command, as an `issues[]` entry in the order, and let it go through the same approval.

## Local preparation progress
At order start, immediately run `fa order progress start --title "<request summary>"`,
then `fa gate watch --open` so the lane view is available while the order is prepared.
Report facts after every preparation step with `fa order progress`; its JSON output is
the persisted state shared with the view. Bubble text is built deterministically from
these facts, with no LLM calls or separate model generation for bubbles.

Use these commands as the corresponding facts become known (replace angle brackets):
- Settled decision: `fa order progress ledger --add "<decision>"`.
- Proposed issue: `fa order progress chunk --add "<chunk>"`. Keep its name stable;
  each chunk has five cells: `goal`, `out_of_scope`, `done_when`, `lane`, `skills`.
- Open question: `fa order progress cell --chunk "<chunk>" --slot <slot> --ask "<question>"`.
- Settled cell: `fa order progress cell --chunk "<chunk>" --slot <slot> --filled`.
  Mark each cell only when its content is settled, including an explicitly empty skills
  list. An unanswered question leaves the cell empty; a filled cell clears its question.
- Acceptance checks written: `fa order progress tests --done <n> --total <N>`;
  count chunks with finished checks out of all chunks that need them. For an issue with
  behaviour statements, its checks are finished only when `fa order prepare PLAN.json --status --json`
  reports it `reviewed`; written but unreviewed tests do not count.
- Successful plan dry-run: `fa order progress check`.
- User approval received: `fa order progress approval`.
- Each issue actually published or updated: `fa order progress publish --issue <number>`.
- After the final summary, or when the user cancels: `fa order progress end` clears
  preparation state. Keep facts on a recoverable failure so preparation can continue.

These commands record facts locally; `progress publish` records the result of publication.
Use `fa order publish` below to perform the approved publication itself.

## 1. Gather before asking
Run `fa order context --json` and read it fully. Then read the repository yourself as needed
(README, existing specs in `docs/specs/`, the code areas you will touch).
- Pending `decisions` and `feedback` come first: they are answers the user already gave.
- Present issue questions only from `decisions`: these belong to this factory's effective
  orderer and are answered in that person's planning session. Reuse the existing @orderer
  GitHub issue comment and decision flow. Keep foreign questions as read-only facts in
  `other_orders`; their orderer's planning session handles the answers. If `identity`
  reports an error, explain it and keep processing the legacy and singleton questions
  retained in `decisions`. Labelled owners remain visible in `other_orders` until the
  factory account is known; do not guess that account. Show any `owner_error` from
  `open_issues` as an unresolved ownership fact.
- Compare `other_orders` Files with every proposed chunk's Files. Show overlap facts
  with links to the existing issues, their orderers and current status; let the person
  choose **wait / split files / go ahead**, with a recommendation and its tradeoff.
  Empty or missing Files means unknown scope, so explain that uncertainty. Preserve
  globs as declared; inspect the repository to explain potential overlaps. The script
  supplies facts and the planning session and person make the decision.
  Before approval, read open night-order and lane-memory issues whose Files overlap
  the `MEMORY.new.md` split and add the real dependency to the plan.
- Build a **decision ledger** from the docs before asking questions: design docs,
  decision logs, README, CLAUDE.md/AGENTS.md and specs contain earlier answers.
  Give each entry its decision, reason, constraints, out of scope, source reference and
  date (say unknown when the source has no date). Label each source: `code`, `docs`,
  `person` or `assumption`; cite a file/section or the person's words. Assumptions stay
  provisional until resolved. Record settled entries with `fa order progress ledger`.
- Once, confirm the person's goal in context as described in Talking to the person.
  Correct it from the person's response; then ask only about gaps. If this session already
  confirmed the goal, reuse it and explain only scope changes in context. Documents fill
  gaps; they do not constrain what the person may request now.
- Existing open issues without the Foragent format: propose per issue — rewrite (contract +
  acceptance test), split, merge duplicates, close (with reason, original kept) or leave
  (not finished by code). Apply only what the user approves; never delete.
- Never ask what the code, docs, specs or earlier decisions already answer.
- A **starter order** (the person says `starter: ...` after `fa init`) is one tiny task
  a lane can finish in about 10 minutes, to show the flow to a merged PR. Their example is
  a suggestion: pick what fits this project from `repo`, `open_issues` and
  `pull_requests` (open and recently merged; `foragent` marks Foragent branches), so it
  neither repeats nor collides with existing work. It goes through the same approval.

### Current words win
The person's current words take precedence over old documents and the ledger.
- When they explicitly change a decision, accept the change without asking permission
  again or persuading them to keep it. Show only the dependent decisions and open issues
  affected by the change, and carry those effects into this order.
- When a request conflicts with a recorded decision but intent to change it is unclear,
  ask once: "D18 says X; this request needs Y" with **Change / Keep / Exception this once**.
  Recommend the option that best fits the current request and explain the tradeoff.
  Apply the answer without repeating the conflict question.
- For Change, update the decision ledger immediately and include both record updates in
  the same approved order. Append a dated replacement with its reason and a "Supersedes"
  link to the old entry in the repository's decision log, or `docs/decisions.md` if none
  exists; leave earlier entries as they are, because the history is append-only. Then
  replace the affected line in `docs/rules.md` with the new rule so readers of current
  rules see it. If the project has no `docs/rules.md`, record the decision only and do
  not create the file. For Exception this once, record its scope in the order spec and
  ledger without replacing the standing rule or touching `docs/rules.md`. For Keep, adapt
  the plan. Persist changes with the approved order so lane agents read the current decision.

## 2. Interview
Native is the default interview. Read `interview.engine` from `fa order context --json`
to choose the resolved engine:
- If `ouroboros`, run the Ouroboros interview (`ooo interview` / `/ouroboros:interview`)
  with the request and settled context from step 1, then run its seed step
  (`ooo seed` / `/ouroboros:seed`). Read the produced seed as settled context and skip
  questions it already answers. Carry the seed into step 3's restatement and confirmation
  and step 4's issue split; continue through steps 3–7.
- If `native`, or if `ouroboros` was selected but its interview or seed step fails, use the native
  AskUserQuestion interview below. Mention the reason once (use `interview.reason` from
  the context when available, otherwise the selected native engine or observed failure).
  Keep any settled answers already gathered and ask only about remaining gaps.
Use Ouroboros only for interview and seed; never use Ouroboros run/evolve (D18).

For the native interview, use AskUserQuestion, **one question at a time**. Offer 2–3
options with your recommendation first and marked, while accepting a free answer.
Reuse the goal confirmation from step 1; if it is still missing, ask the single scope
question described in Talking to the person. Then work on the least clear remaining cell,
using the ledger and code to fill anything you can answer yourself. Keep source labels in
the records; explain the practical tradeoffs and evidence behind options in context.
Refine a free answer into decision / reason / constraints / out of scope, and reflect it
back for correction before treating an uncertain interpretation as settled.
Ask only what changes product behaviour, scope, priority, UX or an approval boundary.
Do not ask about implementation details, and do not decide them in the plan: structure,
internal names, values and data shapes belong to the lane (D98).
- New project (no specs yet): find the goal first — who uses it, the first thing they do,
  what "done for now" means, what is explicitly not in this round.
- Daily order: process decisions/feedback, then the new request. Keep it short.
Keep **open questions** in the progress records; bring the next unresolved decision to
the person with context. The interview ends when no open questions remain and all five
cells per chunk are settled. Stop asking once remaining choices are reversible; settle
the reversible WHAT choices yourself and record the assumptions, and leave HOW to the lane.

### Project profile
Use `profile` from the context and the ledger as earlier answers, not a separate team
interview. On a first order, fill only missing facts: kind (landing, daily-use app, etc.),
stage (building or refining), and priority (speed, appearance or stability). Ask one at a
time when the person alone knows the answer, then run
`fa profile set --kind "<kind>" --stage "<stage>" --priority "<priority>"` to save the
agreed profile with its confirmation date, and add the facts to the ledger.
Distinguish standing decisions from current state such as stage and priority. The `/fa`
session briefing confirms current state once per session; do not ask again on every
order. If evidence suggests a change, propose it with its practical effect. Persist an
agreed project-wide change with `fa profile set`; keep an order-only override in the spec.

### Skills: taste belongs to the person
Read `fa skills catalog --json` for existing and proposed lanes; missing libraries
download automatically. Style skills set the product's voice and visual direction:
confirm style skills with the person, then record the choice in the ledger. Reuse an
already confirmed choice. For an existing lane, apply the agreed fixed choice with
`fa lanes skills <lane> <style-id>`; put it in `new_lanes[].skills` for a new lane.
There can be at most one style skill per lane. Explain the team-wide effect of a change.

Lane agents choose tool, review and domain skills per issue from the approved catalog.
They record the skills actually used in the PR and work log. Leave these choices to the
agent who sees the issue; do not freeze them as lane skills during the interview.
Settle each chunk's skills cell by recording its confirmed style (or none) and this
per-issue selection policy. An empty fixed list does not mean no useful skills exist.

When the person pastes a GitHub skill link, run `fa skills add <url> --json` to preview
it. If nothing in the catalog fits the work, search GitHub yourself, for example with
`gh search repos "<needed capability> skill" --json fullName,url,description,stargazersCount`.
Preview promising candidates with `fa skills add <url> --json`. Show what each does,
its license, stars, whether it contains scripts (`runs_scripts`), and the proposed role
(`style`, `review`, `tool` or `domain`). Tool-specific copies represent one skill.
Ask the person to approve new third-party skills and their roles before registering
them with `fa skills add <url> --approve --role <skill>=<role>` (repeat `--role` for every
skill in the pack). Then refresh the catalog and apply confirmed style choices.
If none fits or discovery is unavailable, state the reason and settle an explicit
no-match or deferred choice with the person; do not invent candidates or hand them commands.

### Roles: lightest fix first
When someone asks for a new standing role (for example "a security owner"), first look
for a cheaper fit and propose it instead. Try these in order and stop at the first that fits:
1. A line in the lane memory: append one `- [id:<unique id>] <lesson>` line to the end of
   `.claude/agent-memory/fa-<lane>/MEMORY.new.md` of the lane that owns the area. The lane
   dream alone folds pending lessons into `MEMORY.md` and records their ids in
   `MEMORY.consumed.ids`; never edit those two files. The orchestrator commits the
   appended line in its next batched commit.
2. A skill attached to the lane that owns the area: `fa lanes skills <lane> <skill-id>`
   (the lanes code is the one writer of lane skills).
3. A reviewer perspective scoped to paths: add a `review_perspectives` entry
   (`{name, focus}`) whose focus names the paths, for example security on
   `src/payment/**`. This needs no new schema.
4. A new role only when the work must judge across areas and send proposals.
In the multiple-choice confirmation, show the cheaper option first, with the reason it
fits; offer the role after it. Only after the person confirms a role, run
`fa role add <name> --scope "<paths>" --signal "paths:<paths>" --skill <skill-id> --why "<confirmed reason>"`
with the name, scope, call conditions (repeat `--signal`) and skills (repeat `--skill`).
Every role may be idle. Roles never message the person; everything goes through the
orchestrator.

## Design first (D33)
Use `ui.frameworks` and `ui.has_design_foundation` from the context alongside the request.
For UI work without a foundation, first suggest the design interview (디자인 인터뷰); if the
person agrees, follow `skills/fa-design-session/SKILL.md` (the `fa-design-session` skill)
as design mode in this same planning session to settle the visual direction (D72).
Compare references on the local board, then running drafts in their actual medium.
Read its `docs/design/<topic>/HANDOFF.md` as settled context, then
create a small design foundation implementation issue: tokens,
a few basic components, a sample page, and `docs/design/CONTRACT.md` for product-wide
tone, density and principles. Derive taste from 2–3 reference apps or sites the user
likes (spacing, density, color count, typography); get one screenshot approval.
Only visual work (colors, spacing, component shapes and layout) is `blocked_by` this
foundation. Backend, API, data, logic and unstyled UI structure with E2E can run in
parallel from the start. Put each screen's audience, task and information hierarchy in
that screen issue's plan; read an existing foundation as settled context.
For visual feedback, ask "this screen or the standard?" If the standard is the problem,
create an issue to fix the tokens or contract so all screens improve together.
Exploratory throwaway prototypes are exempt: label the prototype as disposable, then
establish the foundation and rebuild once the direction is known. Include
`fa design lint` in visual issues' Done when checks, alongside accessibility and flow E2E.
Carry the selected draft's run instructions and the same screen as the draft into
visual Done when evidence so implementing workers can reproduce the agreed result.
Put the approved drafts, runnable prototypes, `HANDOFF.md` and screenshots of the chosen
draft in the plan's `reference_files` under `docs/design/<topic>/`, so they are published
with the spec and given to preread. Visual issues point to the files they need with
`references`; omitting it selects all reference files and `[]` selects none. Keep
third-party research images local.

## 3. Restate and confirm
Before splitting, give the session's summary in a few lines: the person's problem, what
will change for them, and what is out of scope. Before approval, list the decisions the
session settled itself once in this restatement, under "I decided these for you" (in the
person's language), as one short list so the person can change them. Keep the decision
ledger, source labels and chunk cells internal; this is a plain-language summary of choices.
Include the seed when Ouroboros produced one. Reuse the goal confirmation from steps 1–2
if it still fits; otherwise ask the single scope question on the changed result, in context.

## 4. Split into issues
One issue = one PR that can be verified on its own, sized for one agent session.
Split further when any of these differ: the result that can be merged alone, the
verification environment, an approval boundary, or a part that can be blocked while the
rest still completes. For each issue you must be able to write one sentence:
"Close when <specific evidence>". If you cannot, split or ask.
Plans and issues name only the goal, scope, observable behaviour and inter-issue
contracts (what another issue relies on). HOW belongs to the lane: leave structure,
internal names, values and data shapes to the lane, which proposes its approach in
preread and records its choices. Do not prescribe them in the goal, plan or spec.
When several issues depend on each other's code (shared interfaces, or one screen
split into parts), the first issue is a skeleton that defines the shared names,
signatures and data shapes in code. The order spec lists them under a Contracts
heading. Every dependent issue lists the skeleton issue in `blocked_by`.
A skeleton fixes only these real interfaces between issues; it is not a reason to
freeze a lane's private implementation.
- **Lane** = the area of the code it changes. Issues in one lane run one after another;
  lanes run in parallel. Use existing lanes; propose a new lane only for a genuinely new
  area (name, paths, confirmed fixed style skills from `fa skills catalog`). Never put two
  issues that edit the same files into different lanes unless one is blocked by the other.
- **Size** s/m/l = how much reasoning it needs (s: mechanical, m: normal feature,
  l: tricky logic, concurrency, data migration). Size picks the starting model tier.
- **Interactive (D72)**: set per-issue `interactive: true` when completing the work
  clearly needs a lot of back-and-forth with the user, such as repeated visual feedback
  or decisions that depend on seeing intermediate results. Publication adds the
  `interactive` label. Use `false` or omit it for work an agent can finish unattended;
  complexity alone is not a reason to mark an issue interactive. For visual design,
  suggest the design interview (디자인 인터뷰, the `fa-design-session` skill in `/fa`) to
  settle the direction together.
  This order records the choice and suggestion; session startup is a separate workflow.
- **blocked_by** only for real order dependencies (B cannot be built or verified without A).
- Fill each issue's **`files`** with the repository-relative paths or globs it is expected
  to change (for example, `["src/foragent/order.py", "skills/fa-order/**"]`). Use `[]`
  when the affected files are not yet known. The published issue lists these under `## Files`.
- Put the shared outcome and cross-issue constraints in the parent summary, not in children.

Record the lane map before writing acceptance tests: chunk × goal / out of scope /
Done when / lane / skills, with source labels for the settled facts and open cells.
Fill lanes from code paths and the existing lane definitions; ask only when ownership is
unclear or a new lane needs agreement. Settle all five cells, including executable check
commands and the skills policy above. Keep reporting the corresponding `fa order progress`
facts; unanswered cells stay open. Reuse the person's scope confirmation from steps 1–3.

## 5. Write the final checks before any code exists
For every issue, write the acceptance test file(s) now. They are the goal; the implementer
cannot change them. Draft the acceptance tests and the spec in a temporary folder outside the
main checkout (for example `mktemp -d`), never in the shared working tree: other sessions
use that checkout, and its files must stay untouched. Put the final contents into the plan's
`acceptance_files` and `spec_markdown`, under their acceptance-dir paths. Test observable behaviour through the
project's real entry points (CLI, HTTP, public functions), not internals. Locked checks
do not import internal helpers or functions unless the issue explicitly declares that
function as its contract (for example a skeleton signature listed under Contracts), and
they do not pin internal names, values or shapes the lane is free to choose. Each test must
fail today and pass only when the goal is met. Prefer 2–4 sharp cases including one edge.
Judge every test by the break-it rule (from pstack's
principle-test-behavior-not-implementation, MIT): Make the code return a wrong value, skip an
effect, or do the effect twice. If the test still passes, it proves nothing. Rewrite such a
test so that each of those breaks makes it fail.
When a behaviour cannot be checked by a machine (visual taste, real device), add it as a
checklist item and keep at least one executable command anyway (e.g. the build or a smoke
check). Done when commands must run with the repo's normal tooling from the repo root.

## 5b. Challenge the spec from other seats
Once the spec is roughly formed, before you lock it for approval, write the plan JSON
(schema in `fa order publish --help`) to the state drafts folder
`~/.local/share/foragent/<repo>/drafts/<order-id>/plan.json`, never /tmp or a worktree, and run
`fa order perspectives PLAN.json --json`. Read-only agents question it in parallel from
fixed seats: skeptic, first-time user, attacker, operator, maintainer in six months,
incidents (risk map and decisions), and other users. A failed seat is listed under
`failures`; the others still count. Merge duplicate questions across seats into one. Answer what the code, the
docs or a settled decision already answers, and fold it into the spec yourself. Ask the
person only the questions D71 leaves to them: behaviour, scope or approval. Then record
each question once with `fa order perspectives mark ID --changed yes|no` (yes when it
changed the spec; mark every id of a merged duplicate). `fa order perspectives stats --json`
shows which seats changed specs.

## 6. One approval
Keep the plan JSON in that state drafts folder (schema in `fa order publish --help`). Run
`fa order publish PLAN.json --dry-run`, fix validation errors, then record `progress check`.
Then run `fa order preread PLAN.json --json`. The implementer reads each issue read-only and
answers with `APPROACH:` (its design), `DECIDE:` (implementation choices it will make) and
`QUESTION:` lines (unknowns it cannot settle alone). Resolve each question about WHAT (goal,
scope, observable behaviour or an inter-issue contract) from existing evidence: code, docs,
the decision ledger and settled decisions. Fold those answers into the plan. A question
about HOW means the plan said too little or too much: leave that choice to the lane in the
issue text rather than answering it for the lane. Bring the person only the unresolved WHAT
decisions, one at a time with context, and fold their answers into the plan.
DECIDE lines are the lane's own choices: show them once in the restatement you present for
approval, under "the lane will decide these" (in the person's language). They need no
planning approval and no loop to remove every guess; the lane records them in its PR.
`--approve` refuses issues whose latest preread of the current content is missing or still
has open questions; any plan edit needs a new preread. The dry-run is never blocked.
Issues on a lane this order creates (`new_lanes`) have no implementer until the lane exists,
so preread refuses them: after the person's first-hand approval here, publish once without
`--approve` to create the lanes, run preread, then publish again with `--approve`. Reuse that
approval only while the scope is unchanged; get approval here for any changed scope.
Present the session's summary of the person's problem, the result they will get and
what is out of scope. Explain any effect of lane assignments on delivery in their terms;
keep the lane map and implementation details in the progress records.
Before asking for approval, check every issue and fix the plan where a check fails:
- (a) Reuse: does existing code already do this or most of it? If so, why is reusing it
  not enough? Name the measured gap.
- (b) Bar: is the Done-when bar stricter than what the existing proven path meets? If so,
  justify it with an incident.
- (c) Reversibility (principle 10): if this fails, can it be undone? If yes, the Goal asks
  for detection and recovery, not zero races; only irreversible failures (uncommitted work
  deleted, permanent deletion, sending outside) are prevented structurally.
Then AskUserQuestion: Approve all (recommended) / Approve some / Change something.
Show the acceptance test of any issue the user asks about.
Show which issues are interactive and why before asking for approval.
Record `fa order progress approval` once the user approves. If the plan changes, repeat
the dry-run and present the revised scope for approval.

## 7. Publish
For a choice-free fix the orchestrator runs this step itself after a clean preread (the D71
boundary above). After receiving the person's first-hand approval here, this planning session runs the
validated `fa order publish PLAN.json` for real with
`--approve` (or `--approve-keys` for a partial approval). `fa order publish` places the
acceptance files and the spec in the checkout itself: it takes a publish lock, fetches and
fast-forwards to the latest base branch, writes and commits only this order's own paths,
and pushes. Other sessions' edits stay out of the commit, so no manual rebase is needed. If
it cannot fast-forward, report the error instead of rebasing or committing by hand. Decisions and feedback that change
existing issues go into `edits`. Record `progress publish --issue <number>` for each
published or updated issue in the result. After this planning session successfully publishes
a plan the orchestrator drafted, mark the originating request done with
`fa orch request-status ID done --note "<issues>"`, using the originating request ID and the
published issue numbers. A failed publication leaves the request processing.
Show the printed summary and one line on what
happens next ("the gate starts within 5 minutes; /fa shows results"), then run
`fa order progress end`.

After publishing, check the orchestrator. This session is the planning session (D76). It never becomes the orchestrator and never
takes over its work; it only makes sure one runs and talks to it:
- Run `fa orch alive --json`. If it prints `"alive": false`, run `fa orch ensure`. It
  opens one orchestrator in the background and prints one line; when it opened nothing,
  tell the user that line. If the orchestrator is already alive, do not start a second one.
- Open the lane view beside this planning session with `fa gate watch --open`.
- Send every request to the orchestrator twice: as a cross-session message to the
  `orch: <project>` session (SendMessage) and with
  `fa orch request --from planning --text "<request>"`, so it survives a restart.
- While this session is open, renew `fa plan heartbeat` about every minute, so
  questions reach this session by message instead of a push notification.
- Show the open questions from `fa orch questions`, each with its context as in Talking
  to the person, and record answers with
  `fa orch answer <id> --text "<answer>"`.

Never add `approved` yourself outside `fa order publish`, never edit acceptance tests of
issues that are already running, never merge.
