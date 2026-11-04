# Rules in force

This file lists the rules in force now, one line each. Each line cites a live decision in
[decisions.md](decisions.md), which keeps the history and the reasons. Only Jin makes rules ([D108](decisions.md#d108-지금-규칙은-rulesmd-지난-기록은-decisionsmd-2026-10-31-jin)).

## Dashboard and screen

- Settings are asked once at first use, then changed by talking or in the settings screen. ([D70](decisions.md#d70-설정은-처음에-묻고-말로든-설정창으로든-바꾼다-2026-10-22-jin))
- Every word on screen carries its evidence and that evidence's age. "Alive" needs fresh evidence. ([D67](decisions.md#d67-화면의-모든-말은-증거와-그-증거의-나이를-가진다-로그는-정리한다-2026-10-22-jin))
- Stage names say what is being checked, not where: 작성, 준비, 목표, 리뷰, 전체, 머지, 차례. ([D113](decisions.md#d113-단계-이름-2026-11-04-jin-10-27-승인-목록을-바꾼다))
- The dashboard's main screen shows only what is happening now; details for one issue open in a bubble when you tap its acorn. ([D112](decisions.md#d112-대시보드-한-화면에-정보-하나-2026-11-04-jin-d64-보강-5와-10-30-r53을-잇는다))

## Merge

- New main commits that change only files without effect on the result don't restart verification. ([D85](decisions.md#d85-기록과-주문은-모아서-올린다--공장을-흔드는-건-코드가-아닌-커밋이었다-2026-10-24-jin))
- Records and orders reach main in batches. Planning doesn't commit straight to the shared main checkout. ([D85](decisions.md#d85-기록과-주문은-모아서-올린다--공장을-흔드는-건-코드가-아닌-커밋이었다-2026-10-24-jin))
- The merge owner is the only writer of main; others request, and it writes between batches under the main write lock. ([D118](decisions.md#d118-머지-담당은-main의-유일한-작성자다-2026-11-03-jin-결정-2026-11-04-기록))
- The merge owner reuses old evidence only when main moved forward by additions or edits under `.claude/agent-memory/` that the PR did not touch, with `checks: ci`. ([D111](decisions.md#d111-머지-담당은-레인-기억-커밋만-결과에-영향-없는-변화로-본다-2026-11-04-pm-정답-있는-결정))
- A merge batch moves main to its tip in one step when the tip's CI and member checks pass; a failed batch retries with the front half of its members. ([D120](decisions.md#d120-머지-묶음은-통째로-한-번에-넣고-실패하면-절반으로-다시-묶는다-2026-11-0405-jin))

## Orders and approval

- A bug fix with no choice: the orchestrator publishes and approves it after a clean preread. Anything with a choice goes through planning to Jin. ([D101](decisions.md#d101-선택이-없는-버그-수정은-오케스트레이터가-발행하고-승인한다-2026-10-31-jin))
- Planning and the person own WHAT: goal, scope, visible behaviour, contracts between issues. Lanes own HOW: structure, names, values. ([D98](decisions.md#d98-무엇what은-계획이-어떻게how는-레인이-정한다-2026-10-30-jin))
- Preread is the lane's plan. Only `QUESTION:` lines (WHAT ambiguities) block approval. ([D98](decisions.md#d98-무엇what은-계획이-어떻게how는-레인이-정한다-2026-10-30-jin))
- Approval binds the body at approval time. A changed body voids the approval. ([D7](decisions.md#d7-승인은-그-시점의-본문에-대한-것이다))
- When a parent changes after approval, only children not yet started follow it. ([D103](decisions.md#d103-승인-뒤-부모가-바뀌면-아직-시작하지-않은-자식만-따른다-2026-10-31-jin))
- An issue that adds or changes shared state names the one writer of each state in its Goal. Two writers means redesign before approval. ([D95](decisions.md#d95-상태-하나에는-쓰는-쪽이-하나다-2026-10-27-jin))
- Before asking, the interview reads the docs and confirms the decision ledger. It asks only the gaps. ([D28](decisions.md#d28-기존-프로젝트에-들이기brownfield-2026-10-20-jin), [D58](decisions.md#d58-인터뷰는-ouroboros-설계를-가져와-native로-돌리고-그-위에-레인을-얹는다-2026-10-22-jin))
- Before proposing new structure, ask what exactly is slow, how the industry does it, and whether it replaces or adds to the existing path; add one part, and keep evidence makers apart from actors. ([D110](decisions.md#d110-새-구조를-제안하기-전에-묻는-세-질문-2026-11-04-jin))
- A PM fix merges when the latest three-seat review finds no P1; a P2 that can stop the target or let unverified code in counts as P1, other P2s go to a cleanup issue. ([D119](decisions.md#d119-pm-수정의-머지-기준-2026-11-04-jin))
- GitHub issues own the facts about work. Local state is a copy. Memory keeps only lessons. ([D102](decisions.md#d102-일에-대한-사실은-github-이슈가-가진다-2026-10-31-jin))
- git and GitHub are the source of truth; local copies are caches with one writer, changing state is re-read at write time, and on failure Foragent falls back to the default path. ([D109](decisions.md#d109-git과-github가-원본이다-단일-진실-원천-2026-11-04-jin))
- One writer per state; others leave a request. Compute what records can derive. Append, don't overwrite. ([D95](decisions.md#d95-상태-하나에는-쓰는-쪽이-하나다-2026-10-27-jin))
- `docs/rules.md` holds the rules in force. `docs/decisions.md` is append-only history. ([D108](decisions.md#d108-지금-규칙은-rulesmd-지난-기록은-decisionsmd-2026-10-31-jin))

## When to ask the person

- Ask the person only about goal changes, trade-offs, taste, hard-to-undo actions, and scope or priority. Hard to undo means data, money, public exposure or sending to others; changes git can revert, including deleting superseded code, are not person questions. A question an existing decision answers goes to the PM, not the person. Do anything with a right answer, then report it. ([D71](decisions.md#d71-사람에게-묻는-기준-정답이-있는-일은-하고-알린다-2026-10-22-jin), [D115](decisions.md#d115-대체된-코드를-지울지는-사람에게-묻지-않는다-2026-11-04-jin))
- Decisions made with the person go into decisions.md and this file in the same session; memory keeps only a link. ([D114](decisions.md#d114-사람의-결정은-저장소에-바로-넣는다-2026-11-04-jin))
- The person talks to the planning session. The orchestrator works in the back and sends its questions through planning. ([D76](decisions.md#d76-왼쪽은-기획-세션-오케스트레이터는-뒤에서-2026-10-23-jin))
- Only the orchestrator adds `needs-human`, with what it tried, why a person is needed, and options. The gate and lanes add `blocked` with a reason. ([D45](decisions.md#d45-사람-필요는-오케스트레이터만-띄운다-2026-10-21-jin))
- What the person says now beats old docs. Ask once about a conflict, and record a change as a new decision. ([D58](decisions.md#d58-인터뷰는-ouroboros-설계를-가져와-native로-돌리고-그-위에-레인을-얹는다-2026-10-22-jin))
- Telling the person to type a command is a failure signal. Foragent runs it. ([D57](decisions.md#d57-사람이-명령어를-치지-않는다-재설치와-레인-화면은-foragent가-한다-2026-10-22-jin))

## Accounts

- GitHub writes go through `fa` commands, with one permission ask at install. ([D104](decisions.md#d104-github-쓰기는-fa-명령으로-권한은-설치-때-한-번-묻는다-2026-10-31-jin))
- Each person's factory works only the orders that person published (`by:<login>`). ([D78](decisions.md#d78-revision--each-persons-factory-works-that-persons-orders-2026-10-30-jin))

## Tests

- Lanes write the locked tests before publication. The tests lock on approval after review. ([D100](decisions.md#d100-레인이-발행-전에-시험을-쓰고-리뷰-뒤-승인-때-잠근다-2026-10-31-jin))
- Locked tests check behaviour at public boundaries, never undeclared internal helpers. ([D98](decisions.md#d98-무엇what은-계획이-어떻게how는-레인이-정한다-2026-10-30-jin))
- An implementation PR may not change locked tests. ([D11](decisions.md#d11-최종-시험은-구현-전에-정하고-잠근다))
- The gate runs `## Done when` itself and trusts no "it passed". An issue without it does not run. ([D6](decisions.md#d6-완료는-증거로-판정한다))
- Acceptance tests that passed before the run must still pass. A test gone from main is not guarded. ([D53](decisions.md#d53-이미-통과하던-인수-시험이-깨지면-그-이슈는-끝나지-않는다-2026-10-21-세-번째-주문-qa에서-발견), [D66](decisions.md#d66-일이-돌면-지켜보는-세션이-반드시-있다-2026-10-22-jin))
- With CI, the runner runs Done when, the baseline and the changed files' own tests. CI runs the rest. ([D81](decisions.md#d81-ci가-있으면-러너는-최소만-시험한다--설정창에서-바꿀-수-있게-2026-10-24-jin))
- Locked tests of closed issues must stay green on main; a failure is reported, not excused, and is rechecked from CI's existing main results. ([D122](decisions.md#d122-한-번-맞았던-약속은-main이-바뀔-때-기계가-다시-확인한다-2026-11-05-jin))

## Gate and lanes

- The gate judges nothing: it places, cleans up and keeps safety lines. The orchestrator judges order and priority. ([D80](decisions.md#d80-판단은-오케스트레이터-실행은-gate--계획표와-툴-2026-10-23-jin))
- Safety lines outrank the plan: real `blocked_by`, machine budget, locked tests, merge conditions. ([D80](decisions.md#d80-판단은-오케스트레이터-실행은-gate--계획표와-툴-2026-10-23-jin))
- The goals are no idle machine while approved work waits, and little discarded work. Merge count is context, not a goal. ([D96](decisions.md#d96-시간당-머지-수-대신-놀지-않고-버리지-않기-2026-10-27-jin))
- A lane runs any number of issues at once if their `files` don't overlap. Issues with no `files` run one at a time. ([D87](decisions.md#d87-레인당-동시-실행-상한은-없다--파일이-안-겹치면-같이-돈다-2026-10-25-jin-객관식으로-고름))
- Parallel runs follow measured machine capacity. Plan quota is a target to use before reset, not a limit. ([D60](decisions.md#d60-요금제-한도와-컴퓨터-성능이-허락하는-만큼-밀어붙인다-2026-10-22-jin))
- Running work is never cut on a fixed time. A stall is measured by silence: no new output and no CPU growth. ([D89](decisions.md#d89-진행-중인-일은-시간으로-끊지-않는다--멈춤은-조용함으로-잰다-2026-10-25-jin))
- Transient executor errors don't use attempts. A limit error pauses that executor until its reset. ([D24](decisions.md#d24-실행기의-일시-오류는-실패로-세지-않는다-2026-10-20-구현-중-실제-발생))
- Urgent work goes to the front of its lane queue by the orchestrator. Planning builds outside the gate only when the gate itself is broken. ([D83](decisions.md#d83-급한-일은-오케스트레이터가-큐-맨-앞으로--기획-세션의-바깥-구현은-게이트가-고장-났을-때만-2026-10-24-jin))
- The gate raises no macOS permission prompts. Access to protected folders fails quietly. ([D97](decisions.md#d97-게이트는-macos-권한-팝업을-띄우지-않는다-2026-10-29-jin))
- A lane is one agent definition: paths, skills and memory, no persona. ([D22](decisions.md#d22-레인--에이전트--영역-2026-10-20-jin))
- The orch work is split into three lanes: orch, review-engine (리뷰 장치) and roles (역할). ([D107](decisions.md#d107-orch-레인을-셋으로-나눈다-orch-review-engine리뷰-장치-roles역할-2026-10-31-jin))
- Lanes pick tool, review and domain skills per issue from the approved list. The person picks style skills. ([D58](decisions.md#d58-인터뷰는-ouroboros-설계를-가져와-native로-돌리고-그-위에-레인을-얹는다-2026-10-22-jin))

## Review

- Review judges by tests, goal, principles and real defects. Other approaches, names and structure are taste: a NOTE, never a failure. ([D98](decisions.md#d98-무엇what은-계획이-어떻게how는-레인이-정한다-2026-10-30-jin))
- The first review round is multi-perspective. Later rounds only confirm. ([D105](decisions.md#d105-첫-리뷰는-여러-관점으로-다음-회차는-확인만-2026-10-31-jin))
- An unclear review goes verbatim to the implementer, who fixes or answers each point. A new reviewer judges. ([D44](decisions.md#d44-문제는-가장-가까운-층이-먼저-푼다-2026-10-21-jin))
- When the same issue comes back twice for the same defect class, redesign it. ([D106](decisions.md#d106-같은-종류의-결함으로-두-번-돌아오면-다시-설계한다-2026-10-31-jin))
- A PR that touches a risk-map path gets that note and a stricter review. ([D84](decisions.md#d84-리뷰어를-강화한다--프로젝트마다-자라는-위험-지도-2026-10-24-jin))
- The nearest layer solves first: issue problems go to the lane, system problems to the orchestrator, person-only decisions to Jin. ([D43](decisions.md#d43-오케스트레이터가-기술-문제를-먼저-처리한다-2026-10-21-jin), [D44](decisions.md#d44-문제는-가장-가까운-층이-먼저-푼다-2026-10-21-jin))
- Review strength follows reversibility. `always_human` paths, `risk:high`, deploys and public releases go to a person. ([D10](decisions.md#d10-검수-강도는-되돌릴-수-있는-정도로-정한다))
### Review findings are scored (#1651)

- Every reviewer FAIL finding, in any round (single reviewer, panel merge or confirmation), gets one fresh scoring session with the reviewer, run one at a time. The scorer follows skills/review-procedure (the code-review plugin's 0-100 table and false-positive list, Apache-2.0).
- Grounding rule: a finding blocks only when it is grounded (a line quoted from the Goal or Out of scope, a locked acceptance test file that exists under the acceptance directory, or a concrete breaking input) and scores 80 or more. A grounded fatal-path finding (data loss, breaking main, merge without approval, a person-only permission handed to an agent) blocks whenever the path exists, whatever its score. Everything else is a NOTE with its score, kept as a `review-score` event and `runs/issue-N/score-R-K.txt`. A reply without a valid score keeps the FAIL.
- When the scorer says the Goal does not decide the point (`spec_question`), the run stops with a spec-question to the planning side, like a locked-test conflict; it is not rework.
- Panel fact: the perspective panel still runs only on a first round with a risk note; its defaults are rule-documents, obvious-bugs, git-history, earlier-pr-comments, code-comments and fatal-path.
- `review_scoring: false` in .foragent.yml turns scoring off.

## Roles

- Architect review, the docs role, dreams of the order's lanes and the orchestrator retrospective run when an order finishes. ([D99](decisions.md#d99-역할과-꿈은-주문이-끝날-때-깨어난다-2026-10-31-jin))
- The architect only proposes, with numbers as evidence. The orchestrator decides what becomes an order. ([D82](decisions.md#d82-아키텍트는-기본으로-켜진-역할이다--흐름은-오케스트레이터-구조는-아키텍트-2026-10-24-jin--4-④-대체됨d99))
- Cleanup is an order kind, put in the lane that owns the files. Its goal is issues that touch different files, not a rulebook. ([D68](decisions.md#d68-정리는-기본-순환이다-2026-10-22-jin--3-대체됨d99), [D82](decisions.md#d82-아키텍트는-기본으로-켜진-역할이다--흐름은-오케스트레이터-구조는-아키텍트-2026-10-24-jin--4-④-대체됨d99))
- A dream updates its lane's memory: add, drop stale, merge, and settle conflicts by the decisions. ([D75](decisions.md#d75-레인이-끝나면-다람쥐가-꿈을-꾼다-세-층-기억과-정리-2026-10-23-jin--꿈-1-대체됨d99))
- One orchestrator per project. The gate opens one when work runs and none is alive. ([D36](decisions.md#d36-오케스트레이터-세션-사건이-생기면-깨어나-설명하고-결정을-받는다-2026-10-21-jin), [D66](decisions.md#d66-일이-돌면-지켜보는-세션이-반드시-있다-2026-10-22-jin))
- The UX researcher is a default role, next after the merge owner: it uses the real product as a persona to find seams between features. ([D116](decisions.md#d116-ux-리서처는-머지-담당-다음-순서로-당긴다-2026-11-02-결정-2026-11-04-jin-재확인))
- Default skills come in two tiers: general principle skills for every project's roles, and extra ones for Foragent's own lanes. ([D117](decisions.md#d117-기본-스킬-표를-두-층으로-나눈다-2026-11-04-jin-d581652를-넓힌다))

## Product

- Every word Foragent says follows [voice.md](voice.md). ([D61](decisions.md#d61-foragent의-말투-토스-ux-라이팅-5원칙--ste를-한-장으로-2026-10-22-jin))
- Machine formats stay English. Text for people follows the `language` setting. ([D17](decisions.md#d17-언어-설정))
- Claude + Codex and Claude only both work without setup. ([D63](decisions.md#d63-claude만-쓰는-사람도-설정-없이-쓴다-2026-10-22-jin))
- The default `principle-*` skills are vendored and committed with their source, and installed by `fa init`. ([D117](decisions.md#d117-기본-스킬-표를-두-층으로-나눈다-2026-11-04-jin-d581652를-넓힌다))
