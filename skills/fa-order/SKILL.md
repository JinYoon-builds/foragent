---
name: fa-order
description: Interview the user about what to build, split it into lane-assigned issues with executable acceptance tests (plan + goal per issue), get one approval, and publish them for the Foragent gate. Also turns pending decisions and feedback into updated issues. Use for a new project after /fa-init, and whenever the user wants to order new work.
---

# fa-order

You turn one conversation into many small units of work that agents can finish unattended.
Each unit is a GitHub issue: its body is the **plan** (Goal, Out of scope) and its `Done when`
commands are the **goal** that a machine checks. The gate then runs them lane by lane.
The quality of everything that follows is decided here.

Talk to the user in the `language` from `fa order context`. Write issue text, tests and
specs in English unless the repository is clearly written in another language.

## Voice
Read `docs/voice.md` when present and use it for questions, summaries and issue text.
Use short, active sentences with one idea each. In Korean, use 해요체 without excessive
honorifics; in English, sound conversational (contractions are fine). Explain choices
through their effect on the person's work. Run the commands below yourself; people
give requests, links and decisions. In next-step guidance, name only `/fa`.

## 1. Gather before asking
Run `fa order context --json` and read it fully. Then read the repository yourself as needed
(README, existing specs in `docs/specs/`, the code areas you will touch).
- Pending `decisions` and `feedback` come first: they are answers the user already gave.
- Build a **decision ledger** from the docs: design docs, decision logs, README,
  CLAUDE.md/AGENTS.md and specs. Documented decisions are earlier answers. Each entry
  holds the decision, its source (path and section or lines), date (or unknown), and
  whether it is a lasting decision or current state. Follow replacement records so
  superseded decisions stay history rather than becoming current constraints.
- Before asking about gaps, show the documented decisions together once for confirmation
  as "What I read from your docs". Reuse that confirmed ledger throughout the interview;
  ask only about missing information. A new project may have no documented decisions.
  Record new answers in the order spec so the next order can read them.
- Existing open issues without the Foragent format: propose per issue — rewrite (contract +
  acceptance test), split, merge duplicates, close (with reason, original kept) or leave
  (not finished by code). Apply only what the user approves; never delete.
- Never ask what the code, docs, specs or earlier decisions already answer.

### Current words and earlier decisions (D58)
The person's **current words take precedence** over old documents. Documents fill gaps
in the current request; they do not veto it.
- If the person explicitly changes a decision, accept it. Show dependent decisions and
  open issues affected by the change; do not ask permission to change it again.
- If a request conflicts with a documented decision but the intent to change it is
  unclear, ask once: "D18 says X; this request asks for Y" — **Change / Keep /
  Exception this once**, with the recommended choice first. Then use the answer.
- For a lasting change, update the decision record with the order: add a dated
  replacement entry and mark the old entry superseded with a link to the new one.
  Use the existing decision log; if none exists, use `docs/decisions.md`. Show these
  documentation changes in the order approval, then publish them before releasing
  affected issues. `fa order publish` commits only the spec and acceptance files;
  publish decision-log edits through the repository's normal documentation workflow.
  Update the decision ledger and the order spec too.
  Record a one-order exception in the spec without replacing the lasting decision.

## 2. Interview
The default interview is **native** (D58). Read the resolved `interview.engine` from
`fa order context --json` to choose the interview:
- If `ouroboros`, run the Ouroboros interview (`ooo interview` / `/ouroboros:interview`)
  with the request and settled context from step 1, then run its seed step
  (`ooo seed` / `/ouroboros:seed`). Read the produced seed as settled context and skip
  questions it already answers. Carry the seed into step 3's restatement and confirmation
  and step 4's issue split; continue through steps 3–7.
- If `native`, or if either optional Ouroboros step fails, use the native
  AskUserQuestion interview below. Mention the reason once (use `interview.reason` from
  the context when available, otherwise the selected native engine or observed failure).
  Keep any settled answers already gathered and ask only about remaining gaps.
Use Ouroboros only for interview and seed; never use Ouroboros run/evolve (D18).

### Native interview (D58)
Use AskUserQuestion, **one question at a time**. Offer 2–4 options with the recommendation
first and marked, while accepting free answers. First confirm the big picture: the
outcome and its major chunks. Then address the **least clear** unresolved part, using
the code and ledger before asking the person.
Label each item's source: **code**, **docs**, **person**, or **assumption**; include a
file reference for code/docs and the answer/date for person. Keep assumptions visible
rather than presenting them as settled decisions. Refine free answers into **decision /
reason / constraints / out of scope**, and confirm your interpretation in the next
exchange without bundling another unresolved question.
Ask only what changes product behaviour, scope, priority, UX or an approval boundary.
Do not ask about reversible implementation details — decide them and note them in the spec.
- New project (no specs yet): find the goal first — who uses it, the first thing they do,
  what "done for now" means, what is explicitly not in this round.
- Daily order: process decisions/feedback, then the new request. Keep it short.
Keep a visible list of **open questions** in your messages; the interview ends when it is
empty and each chunk has the five answers below. Resolve reversible choices yourself.
Do not use self-rated ambiguity scores or a fixed question count to decide when to stop.

### Project profile, within this interview
Read `profile` from the context and the ledger. On the first order, fill only missing
human choices: kind (landing page, daily-use app, etc.), stage (building, refining, etc.),
and priority (speed, appearance, stability, etc.). Use one question at a time and save
the confirmed values yourself with
`fa profile set --kind '<kind>' --stage '<stage>' --priority '<priority>'`.
Record their source and confirmation date in the ledger. There is no separate team interview.

Lasting decisions persist until changed. Stage and priority are current state:
the `/fa` session briefing confirms them once per session, showing the date and what
the choices change about the work. Reuse that confirmation here; do not ask or show
it again for every order. If current evidence suggests a change (for example, most
recent orders fix bugs), propose it with that evidence. Save confirmed project-wide profile
changes with `fa profile set`; keep an exception for this order in its spec instead.

### Skills: people's style, agents' tools
Read `fa skills catalog --json` for existing and new lanes. The catalog downloads
missing libraries itself. A failed catalog lookup is a tool problem to resolve or
report, not evidence that no skill fits.
- **Style skills** set the product's voice or design direction. Confirm style choices
  with the person, then fix them in the decision ledger and lane assignment (at most
  one style skill per lane). Reuse a documented choice without asking again.
- **Lane agents choose tool, review and domain skills per issue** from the approved
  catalog, based on the issue they actually receive. Do not make the person select
  these for every issue or freeze them into lane style choices. Ask agents to record
  what they used in their work log and PR.
- When a person supplies a GitHub skill link, run `fa skills add <git-url> --json`
  yourself to preview it. Show the discovered skills (tool copies merged), license,
  and whether they contain scripts (`runs_scripts`). Classify each as style, review,
  tool or domain; inspect included scripts before recommending them. After the person
  confirms the pack and roles, run `fa skills add <git-url> --approve` with a repeated
  `--role '<skill-id>=<role>'` for every discovered skill, then refresh the catalog.
- When nothing in the catalog fits a needed capability, search GitHub yourself with
  `gh search repos '<capability> skill' --json fullName,url,description,stargazersCount,license`.
  Inspect candidates and preview the relevant pack with `fa skills add`. Present the
  source link, license, stars, scripts and a plain reason tied to this order. Stars
  describe popularity, not quality. Register a third-party pack only after the person's
  confirmation, then use the approved style assignment or offer its other skills to agents.
  If no suitable candidate remains, record "no fitting skill" with the reason.

### Ready to split
For every chunk, fill a lane map with **Goal / Out of scope / Done when / Lane / Skills**.
Use actual code paths and existing lanes for ownership. The Skills cell records the
confirmed style (or none) and agents' per-issue selection from the approved catalog;
"no fitting skill" with a reason is also a complete answer. Ask only if a new lane is
needed, ownership is ambiguous, or a missing skill needs a person's choice. Resolve
all product gaps and confirm the lane map before writing acceptance tests in step 5.

## Design first (D33)
Use `ui.frameworks` and `ui.has_design_foundation` from the context alongside the request.
For UI work without a foundation, create a small design foundation issue first: tokens,
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

## 3. Restate and confirm
Before splitting, restate in a few lines: what will exist when this order is done, what is
out of scope, and every decision you made on the user's behalf. Include decision
replacements or one-order exceptions. Get an explicit yes; reuse the ledger confirmation
without showing all the documented decisions again. Carry an Ouroboros seed through
this restatement and the issue split when that engine was used.

## 4. Split into issues
One issue = one PR that can be verified on its own, sized for one agent session.
Split further when any of these differ: the result that can be merged alone, the
verification environment, an approval boundary, or a part that can be blocked while the
rest still completes. For each issue you must be able to write one sentence:
"Close when <specific evidence>". If you cannot, split or ask.
- **Lane** = the area of the code it changes. Issues in one lane run one after another;
  lanes run in parallel. Use existing lanes; propose a new lane only for a genuinely new
  area (name, paths, confirmed style from `fa skills catalog`). Leave tool, review and
  domain skill selection to the lane agent for each issue. Never put two
  issues that edit the same files into different lanes unless one is blocked by the other.
- **Size** s/m/l = how much reasoning it needs (s: mechanical, m: normal feature,
  l: tricky logic, concurrency, data migration). Size picks the starting model tier.
- **blocked_by** only for real order dependencies (B cannot be built or verified without A).
- Put the shared outcome and cross-issue constraints in the parent summary, not in children.

## 5. Write the final checks before any code exists
For every issue, write the acceptance test file(s) under the acceptance dir now. They are
the goal; the implementer cannot change them. Test observable behaviour through the
project's real entry points (CLI, HTTP, public functions), not internals. Each test must
fail today and pass only when the goal is met. Prefer 2–4 sharp cases including one edge.
When a behaviour cannot be checked by a machine (visual taste, real device), add it as a
checklist item and keep at least one executable command anyway (e.g. the build or a smoke
check). Done when commands must run with the repo's normal tooling from the repo root.

## 6. One approval
Show the order grouped by lane, work first:
```
This order: 7 issues
 api/  (lane api)   login API → token refresh → payment API     [m, m, l]
 web/  (lane web)   login page → error message → payment page   [m, s, m]
 docs/              README                                      [s]
 runs in parallel: api, web · style: web=<confirmed style>
 other skills: lane agents choose per issue
```
Then AskUserQuestion: Approve all (recommended) / Approve some / Change something.
Show the acceptance test of any issue the user asks about.
Include any decision-record or style-assignment changes in this approval.

## 7. Publish
Write the plan JSON (schema in `fa order publish --help`) to a temp file, run
`fa order publish PLAN.json --dry-run`, fix any validation error, then run it for real with
`--approve` (or `--approve-keys` for a partial approval). Decisions and feedback that change
existing issues go into `edits`. Show the printed summary and one line on what happens next
("the gate starts within 5 minutes; /fa shows results").

Never add `approved` yourself outside `fa order publish`, never edit acceptance tests of
issues that are already running, never merge.
