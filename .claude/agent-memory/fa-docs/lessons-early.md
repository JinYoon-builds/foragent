---
name: lessons-early
description: Early fa-docs lessons (2026-10/11-0x): doc-ownership quirks, decisions.md, README 한국어, drift checks
metadata:
  type: feedback
---

- Orders named as plain titles ('Batches end on silence', #2171) plus an empty '#NNNN: ...' edit order (#2265) can share one child (#2172) landing over several commits; `git log --grep '#child'` and read every merge, mechanisms.md may already be updated by earlier ones.
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
- Prompt-content changes (e.g. #1570, whole authored issue in prompts) touch no reader doc; only docs/impl/*.md (2026-10 phase briefs, e.g. p7c-native.md:9) describe prompt contents. Treat those as history, not drift. Stop-condition orders (e.g. #1750) edit p7c-native.md in the same merge.
- An order issue's title can name another issue (#1614 titled '#1526: review standard'); the merged commit carries the child number, so grep `git log` for both.
- Several scheduled orders can be scope-corrections of one issue ('#1523: review standard, ...'); find the one merge via `git log --grep` on the shared number, then check docs/order.md, which lists every publish refusal.
- Orders titled '#NNNN: lock ...', '#NNNN: re-approve' or '#NNNN: the test owner moves ... locks' (e.g. #1657/#1663/#1665 for #1527, #2133 for #2128) have empty bodies and only move acceptance hashes; check the parent's merge only. Scorecard output fields live in no reader doc (only docs/specs, ideas.md).
- New config keys (e.g. `default_skills`, #1619) need no doc line: README/docs list no config keys (only gate.md mentions max_parallel). Goals can carry Jin-dated license/policy rules; check rules.md and ask for a Dnn.
- Decisions can land as 'Dnn 보강' headings appended by an order's merge (e.g. D105 보강 via #1677), answering an open 'ask for Dnn' wait; grep `^### D.* 보강` before re-asking.
- Lock/serialization orders (e.g. #1661/#1662, all main writers under the merge lock) leave reader docs intact: only how-foragent-differs.md and order.md:150 name the "merge lock", at concept level; D95 already covers one writer.
- GitHub drop/5xx recovery (#1644) lives in no reader doc; rules.md:37 (D24) covers executor transient errors only. Behavior fixes without a dated Goal rule need no doc line.
- Order titles '#NNNN: review standard/license scope' plus a long-titled edit order can all be one issue; check `git log --grep '#NNNN'` once. Vendored library skills (skills/principle-*, pstack MIT) are committed despite rules.md D37 line; check that line when skill-source rules change.
