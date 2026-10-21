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

## 1. Gather before asking
Run `fa order context --json` and read it fully. Then read the repository yourself as needed
(README, existing specs in `docs/specs/`, the code areas you will touch).
- Pending `decisions` and `feedback` come first: they are answers the user already gave.
- Existing design docs, decision logs, README, CLAUDE.md/AGENTS.md and specs are settled
  context. Before asking anything, show a short "What I read from your docs" list for the
  user to confirm, then ask only about gaps the documents do not answer.
- Existing open issues without the Foragent format: propose per issue — rewrite (contract +
  acceptance test), split, merge duplicates, close (with reason, original kept) or leave
  (not finished by code). Apply only what the user approves; never delete.
- Never ask what the code, docs, specs or earlier decisions already answer.

## 2. Interview
Read `interview.engine` from `fa order context --json` to choose the interview:
- If `ouroboros`, run the Ouroboros interview (`ooo interview` / `/ouroboros:interview`)
  with the request and settled context from step 1, then run its seed step
  (`ooo seed` / `/ouroboros:seed`). Read the produced seed as settled context and skip
  questions it already answers. Carry the seed into step 3's restatement and confirmation
  and step 4's issue split; continue through steps 3–7.
- If `native`, or if the Ouroboros interview or seed step fails, use the native
  AskUserQuestion interview below. Mention the reason once (use `interview.reason` from
  the context when available, otherwise the selected native engine or observed failure).
  Keep any settled answers already gathered and ask only about remaining gaps.
Use Ouroboros only for interview and seed; never use Ouroboros run/evolve (D18).

For the native interview, use AskUserQuestion. Multiple choice, your recommendation first
and marked, 2–4 options.
Ask only what changes product behaviour, scope, priority, UX or an approval boundary.
Do not ask about reversible implementation details — decide them and note them in the spec.
- New project (no specs yet): find the goal first — who uses it, the first thing they do,
  what "done for now" means, what is explicitly not in this round.
- Daily order: process decisions/feedback, then the new request. Keep it short.
Keep a visible list of **open questions** in your messages; the interview ends when it is
empty. Stop asking once remaining choices are reversible.

## 3. Restate and confirm
Before splitting, restate in a few lines: what will exist when this order is done, what is
out of scope, and every decision you made on the user's behalf. Get an explicit yes.

## 4. Split into issues
One issue = one PR that can be verified on its own, sized for one agent session.
Split further when any of these differ: the result that can be merged alone, the
verification environment, an approval boundary, or a part that can be blocked while the
rest still completes. For each issue you must be able to write one sentence:
"Close when <specific evidence>". If you cannot, split or ask.
- **Lane** = the area of the code it changes. Issues in one lane run one after another;
  lanes run in parallel. Use existing lanes; propose a new lane only for a genuinely new
  area (name, paths, skills from `fa skills catalog` that clearly fit). Never put two
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
 runs in parallel: api, web · skills: api=testing-strategy
```
Then AskUserQuestion: Approve all (recommended) / Approve some / Change something.
Show the acceptance test of any issue the user asks about.

## 7. Publish
Write the plan JSON (schema in `fa order publish --help`) to a temp file, run
`fa order publish PLAN.json --dry-run`, fix any validation error, then run it for real with
`--approve` (or `--approve-keys` for a partial approval). Decisions and feedback that change
existing issues go into `edits`. Show the printed summary and one line on what happens next
("the gate starts within 5 minutes; /fa-inspect shows results").

Never add `approved` yourself outside `fa order publish`, never edit acceptance tests of
issues that are already running, never merge.
