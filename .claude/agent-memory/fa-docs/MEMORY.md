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
- r320 (2026-11-02): #1614 (#1526 record-only hold causes): no doc edit; r281 review standard read as order-scoped D98 use, folded into r315 D109 question. Wait: orchestrator relays.
- r324 (2026-11-02): #1523 collection check (orders #1522/#1575/#1609/#1622/#1632) — add one paragraph to docs/order.md next to other publish refusals. Wait: orchestrator judges.
- r334 (2026-11-02): #1618/#1647/#1669 (#1619 default skills, merge 52543555): ask Jin for a Dnn for the 2026-11-02 license rule, then one rules.md line. Wait: orchestrator relays.
- r338 (2026-11-03): #1676/#1677 (merge a8d3faea, D105 보강): gate.md:129 drop 'main 유입 포함'; rules.md add PASS-scope line citing D105 (answers r315). Wait: orchestrator judges.
- r347 (2026-11-03): #1684/#1686/#1691 (=#1652, merge 43a39c7b): no doc edit; r334 Dnn ask updated with broader license rule + role table, and rules.md:79 (D37) vs committed pstack principle-* skills. Wait: orchestrator relays to Jin.
- r365 (2026-11-03): orders #1709..#1720 check — Korean README lacks the self-build count line (#1477, 397 PRs/15 days). Wait: orchestrator judges, then docs edits.

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
- Prompt-content changes (e.g. #1570, whole authored issue in prompts) touch no reader doc; only docs/impl/*.md (2026-10 phase briefs, e.g. p7c-native.md:9) describe prompt contents. Treat those as history, not drift.
- An order issue's title can name another issue (#1614 titled '#1526: review standard'); the merged commit carries the child number, so grep `git log` for both.
- Several scheduled orders can be scope-corrections of one issue ('#1523: review standard, ...'); find the one merge via `git log --grep` on the shared number, then check docs/order.md, which lists every publish refusal.
- Orders titled '#NNNN: lock ...' or '#NNNN: re-approve' (e.g. #1657/#1663/#1665 for #1527) have empty bodies and only move acceptance hashes; check the parent's merge only. Scorecard output fields live in no reader doc (only docs/specs, ideas.md).
- New config keys (e.g. `default_skills`, #1619) need no doc line: README/docs list no config keys (only gate.md mentions max_parallel). Goals can carry Jin-dated license/policy rules; check rules.md and ask for a Dnn.
- Decisions can land as 'Dnn 보강' headings appended by an order's merge (e.g. D105 보강 via #1677), answering an open 'ask for Dnn' wait; grep `^### D.* 보강` before re-asking.
- Lock/serialization orders (e.g. #1661/#1662, all main writers under the merge lock) leave reader docs intact: only how-foragent-differs.md and order.md:150 name the "merge lock", at concept level; D95 already covers one writer.
- GitHub drop/5xx recovery (#1644) lives in no reader doc; rules.md:37 (D24) covers executor transient errors only. Behavior fixes without a dated Goal rule need no doc line.
- Order titles '#NNNN: review standard/license scope' plus a long-titled edit order can all be one issue; check `git log --grep '#NNNN'` once. Vendored library skills (skills/principle-*, pstack MIT) are committed despite rules.md D37 line; check that line when skill-source rules change.
- Neutral base-move / evidence-reuse orders (#1687→#1688, run_publish_flow/ci_mode) touch no reader doc: no README/docs line names base moves or neutral moves; only decisions.md D-conflict line mentions rebase.
- 'approve for launch bundle N' orders (e.g. #1708 for #1484) have empty bodies; check the named issue's merge. Role-memory auto-publish (#1699) and the PM seat (#1484, latest `fa` entrance wins) appear in no reader doc; README only says type `/fa`.
- README self-build count (#1477/#1720) is English-only and test_launch_readme.py checks only English; when the count changes, check the 한국어 section too. Batch-merged children (#1158/#1159) carry their own numbers; grep each child, not the parent.
