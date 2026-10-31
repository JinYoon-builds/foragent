# Rules in force

This file lists the rules in force now, one line each. Each line cites a live decision in
[decisions.md](decisions.md), which keeps the history and the reasons. Only Jin makes rules (D108).

## People and questions

- Ask the person only about goal changes, trade-offs, taste, hard-to-undo actions, and scope or priority. Do anything with a right answer, then report it. (D71)
- A bug fix with no choice: the orchestrator publishes and approves it after a clean preread. Anything with a choice goes through planning to Jin. (D101)
- The person talks to the planning session. The orchestrator works in the back and sends its questions through planning. (D76)
- Only the orchestrator adds `needs-human`, with what it tried, why a person is needed, and options. The gate and lanes add `blocked` with a reason. (D45)
- What the person says now beats old docs. Ask once about a conflict, and record a change as a new decision. (D58)
- Telling the person to type a command is a failure signal. Foragent runs it. (D57)
- Settings are asked once at first use, then changed by talking or in the settings screen. (D70)

## Planning and orders

- Planning and the person own WHAT: goal, scope, visible behaviour, contracts between issues. Lanes own HOW: structure, names, values. (D98)
- Preread is the lane's plan. Only `QUESTION:` lines (WHAT ambiguities) block approval. (D98)
- Lanes write the locked tests before publication. The tests lock on approval after review. (D100)
- Locked tests check behaviour at public boundaries, never undeclared internal helpers. (D98)
- An implementation PR may not change locked tests. (D11)
- The gate runs `## Done when` itself and trusts no "it passed". An issue without it does not run. (D6)
- Approval binds the body at approval time. A changed body voids the approval. (D7)
- When a parent changes after approval, only children not yet started follow it. (D103)
- An issue that adds or changes shared state names the one writer of each state in its Goal. Two writers means redesign before approval. (D95)
- Before asking, the interview reads the docs and confirms the decision ledger. It asks only the gaps. (D28, D58)

## Gate and lanes

- The gate judges nothing: it places, cleans up and keeps safety lines. The orchestrator judges order and priority. (D80)
- Safety lines outrank the plan: real `blocked_by`, machine budget, locked tests, merge conditions. (D80)
- The goals are no idle machine while approved work waits, and little discarded work. Merge count is context, not a goal. (D96)
- A lane runs any number of issues at once if their `files` don't overlap. Issues with no `files` run one at a time. (D87)
- Parallel runs follow measured machine capacity. Plan quota is a target to use before reset, not a limit. (D60)
- Running work is never cut on a fixed time. A stall is measured by silence: no new output and no CPU growth. (D89)
- Transient executor errors don't use attempts. A limit error pauses that executor until its reset. (D24)
- Acceptance tests that passed before the run must still pass. A test gone from main is not guarded. (D53, D66)
- With CI, the runner runs Done when, the baseline and the changed files' own tests. CI runs the rest. (D81)
- New main commits that change only files without effect on the result don't restart verification. (D85)
- Urgent work goes to the front of its lane queue by the orchestrator. Planning builds outside the gate only when the gate itself is broken. (D83)
- The gate raises no macOS permission prompts. Access to protected folders fails quietly. (D97)
- A lane is one agent definition: paths, skills and memory, no persona. (D22)
- The orch work is split into three lanes: orch, review-engine (리뷰 장치) and roles (역할). (D107)
- Lanes pick tool, review and domain skills per issue from the approved list. The person picks style skills. (D58)

## Review

- Review judges by tests, goal, principles and real defects. Other approaches, names and structure are taste: a NOTE, never a failure. (D98)
- The first review round is multi-perspective. Later rounds only confirm. (D105)
- An unclear review goes verbatim to the implementer, who fixes or answers each point. A new reviewer judges. (D44)
- When the same issue comes back twice for the same defect class, redesign it. (D106)
- A PR that touches a risk-map path gets that note and a stricter review. (D84)
- The nearest layer solves first: issue problems go to the lane, system problems to the orchestrator, person-only decisions to Jin. (D43, D44)

## State and records

- GitHub issues own the facts about work. Local state is a copy. Memory keeps only lessons. (D102)
- One writer per state; others leave a request. Compute what records can derive. Append, don't overwrite. (D95)
- GitHub writes go through `fa` commands, with one permission ask at install. (D104)
- Every word on screen carries its evidence and that evidence's age. "Alive" needs fresh evidence. (D67)
- `docs/rules.md` holds the rules in force. `docs/decisions.md` is append-only history. (D108)
- Records and orders reach main in batches. Planning doesn't commit straight to the shared main checkout. (D85)

## Roles

- Architect review, the docs role, dreams of the order's lanes and the orchestrator retrospective run when an order finishes. (D99)
- The architect only proposes, with numbers as evidence. The orchestrator decides what becomes an order. (D82)
- Cleanup is an order kind, put in the lane that owns the files. Its goal is issues that touch different files, not a rulebook. (D68, D82)
- A dream updates its lane's memory: add, drop stale, merge, and settle conflicts by the decisions. (D75)
- One orchestrator per project. The gate opens one when work runs and none is alive. (D36, D66)

## Product

- Every word Foragent says follows [voice.md](voice.md). (D61)
- Machine formats stay English. Text for people follows the `language` setting. (D17)
- Claude + Codex and Claude only both work without setup. (D63)
- Review strength follows reversibility. `always_human` paths, `risk:high`, deploys and public releases go to a person. (D10)
- Library skills are copied in at run time, not committed. (D37)
- Each person's factory works only the orders that person published (`by:<login>`). (D78)
