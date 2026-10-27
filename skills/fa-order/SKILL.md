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
  count chunks with finished checks out of all chunks that need them.
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
- Build a **decision ledger** from the docs before asking questions: design docs,
  decision logs, README, CLAUDE.md/AGENTS.md and specs contain earlier answers.
  Give each entry its decision, reason, constraints, out of scope, source reference and
  date (say unknown when the source has no date). Label each source: `code`, `docs`,
  `person` or `assumption`; cite a file/section or the person's words. Assumptions stay
  provisional until resolved. Record settled entries with `fa order progress ledger`.
- Show the collected ledger once for confirmation, as one concise list. Correct it from
  the person's response; then ask only about gaps. If this session already confirmed
  the ledger, reuse it and show only changes. Documents fill gaps; they do not constrain
  what the person may request now.
- Existing open issues without the Foragent format: propose per issue — rewrite (contract +
  acceptance test), split, merge duplicates, close (with reason, original kept) or leave
  (not finished by code). Apply only what the user approves; never delete.
- Never ask what the code, docs, specs or earlier decisions already answer.

### Current words win
The person's current words take precedence over old documents and the ledger.
- When they explicitly change a decision, accept the change without asking permission
  again or persuading them to keep it. Show only the dependent decisions and open issues
  affected by the change, and carry those effects into this order.
- When a request conflicts with a recorded decision but intent to change it is unclear,
  ask once: "D18 says X; this request needs Y" with **Change / Keep / Exception this once**.
  Recommend the option that best fits the current request and explain the tradeoff.
  Apply the answer without repeating the conflict question.
- For Change, update the decision ledger immediately and include the decision record
  update in the approved order: append a dated replacement with its reason and mark the
  old entry superseded, linking the two. Use the repository's decision log, or
  `docs/decisions.md` if none exists. For Exception this once, record its scope in the
  order spec and ledger without replacing the standing rule. For Keep, adapt the plan.
  Persist changes with the approved order so lane agents read the current decision.

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
First show the big picture as proposed chunks and confirm that it matches the request.
Then work on the least clear remaining cell, using the ledger and code to fill anything
you can answer yourself. Show its source labels and the evidence behind the options.
Refine a free answer into decision / reason / constraints / out of scope, and reflect it
back for correction before treating an uncertain interpretation as settled.
Ask only what changes product behaviour, scope, priority, UX or an approval boundary.
Do not ask about reversible implementation details — decide them and note them in the spec.
- New project (no specs yet): find the goal first — who uses it, the first thing they do,
  what "done for now" means, what is explicitly not in this round.
- Daily order: process decisions/feedback, then the new request. Keep it short.
Keep a visible list of **open questions** in your messages; the interview ends when it is
empty and all five cells per chunk are settled. Stop asking once remaining choices are
reversible; decide those yourself and record the assumptions.

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
1. A line in the lane memory: edit `.claude/agent-memory/fa-<lane>/MEMORY.md` of the lane
   that owns the area directly. The orchestrator commits it in its next batched commit.
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

## 3. Restate and confirm
Before splitting, restate in a few lines: what will exist when this order is done, what is
out of scope, and every decision you made on the user's behalf. Include the seed when
Ouroboros produced one. Reuse the big-picture confirmation from step 2 if it still fits;
otherwise get an explicit yes on the changed scope.

## 4. Split into issues
One issue = one PR that can be verified on its own, sized for one agent session.
Split further when any of these differ: the result that can be merged alone, the
verification environment, an approval boundary, or a part that can be blocked while the
rest still completes. For each issue you must be able to write one sentence:
"Close when <specific evidence>". If you cannot, split or ask.
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

Show the lane map before writing acceptance tests: chunk × goal / out of scope /
Done when / lane / skills, with source labels for the settled facts and open cells.
Fill lanes from code paths and the existing lane definitions; ask only when ownership is
unclear or a new lane needs agreement. Confirm the map once all five cells are settled,
including executable check commands and the skills policy above. Keep reporting the
corresponding `fa order progress` facts; unanswered cells stay open.

## 5. Write the final checks before any code exists
For every issue, write the acceptance test file(s) now. They are the goal; the implementer
cannot change them. Draft the acceptance tests and the spec in a temporary folder outside the
main checkout (for example `mktemp -d`), never in the shared working tree: other sessions
use that checkout, and its files must stay untouched. Put the final contents into the plan's
`acceptance_files` and `spec_markdown`, under their acceptance-dir paths. Test observable behaviour through the
project's real entry points (CLI, HTTP, public functions), not internals. Each test must
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
(schema in `fa order publish --help`) to a temp file and run
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
Write the plan JSON (schema in `fa order publish --help`) to a temp file. Run
`fa order publish PLAN.json --dry-run`, fix validation errors, then record `progress check`.
Then run `fa order preread PLAN.json --json`. The implementer reads each issue read-only and
lists what it would have to guess. Show every unknown to the user as it is and ask them to
answer it; never answer an unknown yourself. Fold the user's answers into the plan, then run
preread again until every issue has no unknowns.
`--approve` refuses issues whose latest preread of the current content is missing or has
unknowns; any plan edit needs a new preread. The dry-run is never blocked.
Issues on a lane this order creates (`new_lanes`) have no implementer until the lane exists,
so preread refuses them: publish once without `--approve` to create the lanes, run preread,
then publish again with `--approve`.
Show the order grouped by lane, work first:
```
This order: 7 issues
 api/  (lane api)   login API → token refresh → payment API     [m, m, l]
 web/  (lane web)   login page → error message → payment page   [m, s, m]
 docs/              README                                      [s]
 runs in parallel: api, web · skills: api=testing-strategy
```
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
Run the validated `fa order publish PLAN.json` for real with
`--approve` (or `--approve-keys` for a partial approval). `fa order publish` places the
acceptance files and the spec in the checkout itself: it takes a publish lock, fetches and
fast-forwards to the latest base branch, writes and commits only this order's own paths,
and pushes. Other sessions' edits stay out of the commit, so no manual rebase is needed. If
it cannot fast-forward, report the error instead of rebasing or committing by hand. Decisions and feedback that change
existing issues go into `edits`. Record `progress publish --issue <number>` for each
published or updated issue in the result. Show the printed summary and one line on what
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
- Show the open questions from `fa orch questions` and record answers with
  `fa orch answer <id> --text "<answer>"`.

Never add `approved` yourself outside `fa order publish`, never edit acceptance tests of
issues that are already running, never merge.
