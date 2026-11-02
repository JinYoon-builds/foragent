# fa-docs memory

## Proposals
- r26 (2026-10-29): README 한국어 section rewrite to match English (install, limits, Codex-only), README Install adds starter order + API-key spend_cap, gate.md §4/§8 parallel lines + prune §12/운영 메모.
- r27 (2026-10-29): decision — default executor is codex (config.py) but README/launch posts say gate starts Claude Code sessions; recommended A: make claude default.
- r39 (2026-10-30): decisions.md D55 heading '— 미구현' → 보강 implemented as `fa lanes talk` (#154); squirrel-click wiring still open. #942 watch pid: no doc impact.

- r63 (2026-10-31): docs/rules.md split (r62) — sequence after #1025/team-mode-v1/what-how; D99/D100 for architect-at-order-finish & lanes-write-tests; reader switch in order_perspectives/orch_refresh/orch_dream_worker/fa-order/fa-docs; no line cap.
- r82 (2026-10-31): PR #1224 wrote docs/rules.md (55 rules) + appended D99-D108; flagged #1154 D99 double-append, D100/D106/D99 wording to confirm.
- r83 (2026-10-31): Jin confirmed D100/D99; pushed D106 reword (same defect class twice → redesign) to #1224 as 07462d6c.
- r264-r266 (2026-11-02): r264 docs order (delete status/contract-proposal/trials, needs-human D45, D100 test author, gate.md stale lines, README English docs list); r265 fa-docs SKILL still cites removed docs perspective; r266 question: rules.md:34 vs #448 overlap limit. Wait: orchestrator judges; r266 needs Jin via planning.
- r274-r275 (2026-11-02): docs check #1154/#1156/#1203: tidy cycle vs D99 deletion order, gate.md:152 dream wording. Wait: orchestrator judges.
- r315 (2026-11-02): #1598/#1617 (#1599 PASS-scope review): gate.md already updated; asked Jin for D109 to record the 2026-11-02 review standard before a rules.md line. Wait: orchestrator relays, then docs edits rules.md.

## Lessons
- Since #1167 no default review perspective flags docs; find drift by reading merged diffs against docs. Order numbers are issue numbers, and merge titles usually carry a different PR number.
- gate.md/contract.md/inspect.md are 2026-10-20 designs: expect later decisions (D45 labels, D100 test author) not to reach them.
- decisions.md Korean uses plain 한다 register; Jin's confirmations may come in 해요 — keep the file's register.
- Edit PR branches in a /tmp detached worktree, not the shared main checkout.
- There is no README.ko.md; Korean lives in the `## 한국어` section of README.md and drifts behind the English part (was stale after #849).
- Check launch/README claims against config.py DEFAULTS and init.py's new-config branch; "which tool implements" drifts (default executor is codex unless only Claude is logged in).
- docs/gate.md is a 2026-10-20 design doc; its concurrency/scheduler lines go stale as AIMD (max_parallel) evolves.
- decisions.md headings carry '— 미구현' suffixes that nobody flips on merge; #1025 (fa decisions --json) removes them — after it merges, use that instead of headings.
- Decision IDs cited in specs (e.g. D98) may exist only in an unmerged order's spec; grep decisions.md before citing.
- Most of a '11 docs changes' signal is `docs/specs` order commits ("Foragent order ...: acceptance tests and spec"), not reader docs; filter them out first.
- rules.md lines must cite `### Dnn` headings; verify with a loop over cited IDs. Feature specs (UI details) and one-time events (D88/D90/D94) are not rules.
- Order specs may say "append Dnn in this issue" (e.g. #1154 for D99); check docs/specs before appending a decision, or flag the duplicate.
- An order's parent issue can stay OPEN after its children merge; read the child issues' Goal and their merge diffs (children list is in the parent body).
- When a decision lists what it replaces ('대체하는 것'), grep code and skills for the old trigger too; D99 replaced D68-3 but tidy.post_if_due and the SKILL section stayed.
- A closed child is not always merged: check `stateReason` and the last comment first (#1142, #1171 closed NOT_PLANNED, replaced by other issues).
- Check doc claims against origin/main after a fetch; the shared checkout can lag behind.
- When a change takes over a rendered artifact (e.g. #1372 and the PR body), grep docs for what people do by hand on it; inspect.md's 'tick the box' went stale without a mention of #1372.
- A bug-fix order can add an exception to a rules.md line with no decision ID (#1597, merge-turn owner vs machine budget lines 32/35): grep rules.md for the resource it touches, then ask, don't edit.
- 'Preread answers' issues (e.g. #1156, #1203) have no doc effect; read the plan's acceptance map to find the real issues (#997, #999).
- Order Goals can carry a dated rule ('Review standard (2026-11-02)' in #1599) with no decisions.md entry; check whether it changes a rules.md line and ask for a Dnn.
