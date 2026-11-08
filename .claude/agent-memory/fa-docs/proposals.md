---
name: docs-proposals
description: Log of fa-docs proposals (request ids, dates, wait conditions) sent via fa orch request
metadata:
  type: project
---

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
- r368-r369 (2026-11-03): #1512/#1697/#1718 (=#1469, merge c69e7f98): r368 fa-architect SKILL lacks gate-alarm start + dig method; r369 ask Dnn for alarm/worse-only/no-slot-wait rules. Wait: orchestrator.
- r371 (2026-11-03): #1735/#1736 (merge a7af72be): README Bash(fa:*) paragraph (en 33 / ko 111) add one sentence that FA-launched sessions refuse person-only commands. Wait: orchestrator judges.
- r383 (2026-11-03): #1724/#1725 (merge a0ef6cf8): add `fa order red` paragraph to docs/order.md near preread (~171), folded with r324. Wait: orchestrator judges.
- r449 (2026-11-04): #1746/#1751/#1767 (=#1651, merge 826313b): ask D109 for undated-ID decisions.md:944 + rules.md:82-88 uncited section; gate.md:130 stale 4 defaults, :172 stray yaml; skills/review-procedure vs D37 → r347. Wait: orchestrator relays.
- r482 (2026-11-04): answers r480 (decisions by topic): A = regroup rules.md into Jin's 6 topics + Dnn links + in-place 대체됨 marks; B = top section in decisions.md (needs D108 보강). Wait: orchestrator judges; PM then appends phase words/r53/one-screen Dnn.
- r672 (2026-11-04): orders #1634/#1837/#1958 — README:81/:159 terminal list add Ghostty (#1959). Wait: orchestrator judges, then docs edits.
- r716 (2026-11-05): orders #1790/#1847 (=#1791, merge 96b7dfd5) — add plan `closes` bullet to docs/order.md §6 after :103. Wait: orchestrator judges.
- r720 (2026-11-05): order #1861 (files-scope for #1860, merge 902e7d09 #1882) — rules.md lacks D109-D122; D117 vs rules.md:79 D37 (folds r347); D109 README/CONTRIBUTING line. D109 ID now taken: r315/r449 asks need new IDs. Wait: orchestrator judges.
- r749/r759/r767/r776: closed 2026-11-05 by docs commit 23d3c6f4 (README fa review via /fa, order.md #1206 PATH, review rule D125→D126 incl. gate.md:149); report r777.
- r780 (2026-11-05): #2093/#2094 (merge 6bf24992, q14 exempt scope per path) — gate.md:129 rewrite (also r338's unapplied part), ask D105 보강 2 + rules.md PASS-scope line. Wait: orchestrator judges/relays.
- r800 (2026-11-05): orders #2130/#2135 (#1874 merge b0fedd49, #2136 merge b0985e42) — mechanisms.md 'Order completion' add UX researcher order/daily calls + Origins #1654/#1874. Wait: orchestrator judges.
- r804 (2026-11-05): order #2102 (#2103, merge 14c7afc3) — rules.md add D124 line after :59 (+ optional mechanisms.md:23 clause). Wait: orchestrator judges.
- r449: closed by #1856 (merge 04a16091, D125 review-scoring lines in rules.md/gate.md).
- r819 (2026-11-06): orders #1785/#1795/#1846/#2177/#2181 — npx foragent E404 (npm unpublished, Jin A publish/B curl-only); then launch posts hn/geeknews en+ko install lines → one-line install + /fa. Wait: orchestrator relays.
- r838 (2026-11-06): orders #1033..#2193 — mechanisms.md 'Facts and the architect' add #2125 orchestrator stall signals sentence + orch_facts_stall.py/Origins. Wait: orchestrator judges, then docs applies.
- r859 (2026-11-06): 13 Jin 11-05 decisions are ID-less bullets under D125 (decisions.md:1032-1043) and D127 (:1049); asked A in-place Dnn/보강 headings, then rules.md lines (D87 보강 r266 text, D124 보강 folds r804, D126 보강, write-has-purpose, D123). Wait: orchestrator judges.
- r324/r383/r420: closed 2026-11-04 by order #1826 (child #1827, merge 9f883b2), docs/order.md end paragraphs.
- r863 (2026-11-06): orders #2225/#2238 — docs/order.md:195 Done when executable line add `uv run X` lookup + suggest-only-if-in-venv (#2239). Wait: orchestrator judges, docs applies after no batch-merge PR.
- r868 (2026-11-06): orders #1421/#1895/#2092 (#1422 CI base cache, merge 861e8dc9) — fa-init SKILL:104-107 add sentence: workflow also runs base job on base-branch push + caches. Wait: orchestrator judges.
- r895 (2026-11-06): orders #2077/#2162/#2218/#2292/#2294 — rules.md:10 stage list 머지→합침 (D113 r560 보강 via #2078) + optional mechanisms.md 'merge train'. Wait: orchestrator judges.
- r914 (2026-11-07): r912 check — #2383 mechanisms.md text matches code; applying r859/r863/r868/r895/r904-r905 waits on batch PR #2405 + open #2365/#2390 (rules.md). Wait: docs, after those close.
- r1043 (2026-11-07): #2161 orders — order.md:107 + design/CONTRACT.md:5 still cite docs/specs; skills fa-order:99/fa-docs duty 4. Wait: orchestrator; docs applies after batch PR #2522.
- r904/r905 (2026-11-07): orders #2307/#2309 — mechanisms.md 'Planning and the orchestrator' add #2308 usage-limit push+wake sentence; folds unapplied r838 (stall signals). Wait: orchestrator judges, docs applies.
- r936 (2026-11-07): orders #2271..#2397 — docs/order.md 'Background preread' add #1488 gate-worker paragraph (orch_jobs.py). Wait: orchestrator judges, docs applies.
- r978 (2026-11-07): orders #2412/#2417/#2419/#2423 — mechanisms.md 'Review' add repeated-class hold sentence (#2418) + Origins. Wait: orchestrator judges, docs applies after batch PR #2432.
- r947 (2026-11-07): orders #1513..#2355 — mechanisms.md Docs entry add rule `Check:` + decision-rule/remaining markers (#2356/#2357); fa-docs SKILL duty 5 Check: line. r859/r895 unblocked, add Check: per line. Wait: orchestrator judges.

- r1082 (2026-11-07): orders #2171/#2265 (#2172 silence, PR #2541) — mechanisms.md merge entry: batch of one + single-path turn yield; re-asks r804/r859 D124 rules.md line with Check:. Wait: orchestrator judges; docs applies after batch PR #2593.
- r1098 2026-11-07: #2549/#2562/#2570/#2573 check; one mechanisms.md edit for #2574. Wait: orchestrator.
- r1137 2026-11-08: orders #2214/#2438/#2476/#2556; r947 wording fix for #2477 link events. Wait: orchestrator; after PR #2647.
- r1143 2026-11-08 #2454 promises → mechanisms.md Planning; wait orchestrator, after PR #2665
- r1209 2026-11-08 #2548 mechanisms.md Memory snapshot sentence; wait orchestrator
