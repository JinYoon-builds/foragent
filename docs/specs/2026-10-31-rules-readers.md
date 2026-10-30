# Readers use docs/rules.md (r62/r63)

## Scope and cause

This is only r63 point (4), requested by Jin in r62 on 2026-10-31. Current rules and superseded history are currently supplied from the same document. `src/foragent/order_perspectives.py` hardcodes `DECISIONS_FILE = "docs/decisions.md"` and cuts full bodies to the latest 20 decision entries. `orch_refresh.first_prompt` item 2 names that history, and the dream prompt gives it authority in both its opening and its conflict instruction. An agent lacks a reliable current-rule source; no new scripted judgment about which decisions still apply is needed.

The docs role will write `docs/rules.md` later. This order does not create it, edit `docs/decisions.md`, assign D-numbers, or depend on the docs role's other orders. Acceptance tests create their own documents in temporary repositories. No implementation is included in this draft.

## Required outcome

- When `docs/rules.md` exists, the readers use it as the source of rules in force now. Historical decision contents are excluded from that current-rule context. An existing empty rules file is still selected.
- When it is missing, use `docs/decisions.md`, including in adopted and newly initialized projects. Preserve existing behaviour when both documents are absent. No adoption/init change is required.
- For order perspectives, supply rules.md whole, without the latest-20 cut or cut notice. Preserve all titles, the latest 20 full bodies, and the existing notice for the decisions.md fallback. Keep the risk map and other seats' behaviour.
- For refresh, item 2 names the selected document: rules.md normally, decisions.md for the missing-file fallback. When rules exist, decisions.md is an optional source only when a reason is needed. Keep the handoff and all other required readings.
- For dream, both the authority statement and the lesson-conflict instruction name the selected document. Keep the existing dream lifecycle, memory scope and media behaviour.
- The marker-in-prompt requirement is interpreted literally: refresh and dream also supply the selected document's actual text in their generated prompts, rather than only naming its path. This is an explicit assumption because these two prompts currently contain only paths. Resolve the source in the target repository or dream worktree, not an unrelated launcher directory. No new shared helper or function signature is prescribed.
- In fa-order's Change step, append the dated replacement and its reason, with its supersedes link, to decisions.md AND replace the affected current-rule line in rules.md in the same approved order. Existing history remains append-only. For projects without rules.md, keep recording decisions as today; this reader order does not create a rules document for them.
- In fa-docs, describe rules.md as rules in force now and decisions.md as append-only history. As a duty at each order finish, keep rules.md short by replacing the affected current-rule line when a decision changes or is superseded. This is skill prose, not a new hook, cap or script.
- Add a link to rules.md in the README document list, retaining the history link.

## Lane map: three independent size-s issues

`.foragent.yml` lists exact `src/foragent/order.py` and `config.py` under order; it does not own `order_perspectives.py`. Its order lane and design lane both include `skills/fa-order/**`; use order for this procedure. The orch lane owns `src/foragent/orch*.py`. There is no docs lane: docs is a role, with README scope; fa-docs prose and README therefore go to implicit main with the unowned perspective reader.

| Key | Lane | Files |
| --- | --- | --- |
| current-context | main | src/foragent/order_perspectives.py; skills/fa-docs/SKILL.md; README.md |
| change-record | order | skills/fa-order/SKILL.md |
| orch-context | orch | src/foragent/orch_refresh.py; src/foragent/orch_dream_worker.py |

These are the fewest issues that keep each Files list within one eligible lane. Leave `.foragent.yml` unchanged. All issues are size s and noninteractive; blocked_by is empty because none needs another issue's code. Source selection is a file-availability fact; agents retain judgment over which rules to replace. Do not add keyword/regex classifiers, line caps, LLM decisions in scripts, or a new agent step sequence.

## Locked acceptance tests and delivery

The draft contains only `order-out/plan.json` and three `order-out/tests/acceptance/test_rules_readers_*.py` files. The plan embeds each file verbatim under its eventual `tests/acceptance/` path. Every new module starts with the required locked header. Main checks whole rules text, history exclusion, fallback cutting, an empty rules file, docs ownership and the README link. Order checks the Change paragraph. Orch checks refresh via the real offline CLI/launcher seam and dream through the real worker with fake worktree preparation and an executor that captures the prompt, launched from an unrelated directory; both cases assert actual document markers and the missing-file fallback. Fixtures/style come from test_order_perspectives, test_orch_refresh, test_lane_dream, test_lane_memory_writers and tests/conftest.py. Normal published Done-when commands run from the repository root.

## Verification against supplied main snapshot

HEAD: `2a55cc4b11c706896797094d5827ed3e19b53ee1` (detached supplied checkout). No implementation changes were made.

New drafts: 9 failed, 1 passed. All failures are assertions about the requested feature, not collection/setup errors. Failure locations in the draft:

- `test_rules_readers_main.py:29`: whole rules text is absent; the prompt contains the history marker instead.
- `test_rules_readers_main.py:63`: an existing empty rules file still leaks the history marker.
- `test_rules_readers_main.py:72`: docs skill does not mention rules.md.
- `test_rules_readers_main.py:84`: README has no rules.md link.
- `test_rules_readers_order.py:14`: Change does not mention rules.md.
- `test_rules_readers_orch.py:42`: refresh's current-rules case still says `2. docs/decisions.md`.
- `test_rules_readers_orch.py:43`: refresh's missing-rules case supplies no history marker (the literal-context assumption).
- `test_rules_readers_orch.py:87`: dream's current-rules case still names decisions.md.
- `test_rules_readers_orch.py:88`: dream's missing-rules case supplies no history marker (the literal-context assumption).

The one passing draft test proves that incidents already preserves all decision titles and the latest-20 bodies when rules.md is missing.

Related existing checks: 136 passed, 6 failed across test_order_perspectives.py, test_orch_refresh.py, test_lane_dream.py, test_lane_memory_writers.py, test_orch_refresh_unit.py, test_orch_dream_unit.py, and test_order.py::test_perspectives_decisions_cut_keeps_titles_and_latest_twenty. All six existing failures are in test_lane_memory_writers.py and predate this order: line 30 (two parameter cases) expects pending memory to reach lane readers; line 43 expects MEMORY.new.md in the dream prompt; line 82 (two cases) says "Dream lacks its pending-memory input and fold/clear authority"; line 121 says "Other projects need a supported migration entrypoint". Do not repair that separate feature here or hide these failures. The Done-when commands include the related passing modules; this baseline failure is explicitly reported.

## Existing locked expectations and conflicts

No locked file was edited. A blind global rename would conflict with these unchanged tests:

- `tests/acceptance/test_orch_refresh.py:431`: `for reading in ("docs/decisions.md", "fa orch queue", "fa orch plan",`.
- `tests/acceptance/test_lane_dream.py:194` explicitly includes `"docs/decisions.md"` in the prompt paths; line 204 labels the regex assertion `"decisions win"` and requires decisions.md as authority.
- `tests/acceptance/test_order_perspectives.py:231`: `assert DECISIONS.strip() in prompts["incidents"]` (its fixture writes decisions.md at line 71).
- The nonlocked `tests/test_order.py:1219` also requires `"Cut notice: docs/decisions.md has 25 decisions"`.

All these fixtures omit rules.md, so the requested missing-file fallback reconciles them. They currently pass. Keep them unchanged and rerun them after implementation. There is no unresolved reader-feature conflict; the six pending-memory baseline failures remain an unrelated limitation.

## Commands and structural validation actually run

Interpreter: `/Users/apple/Dev/foragent/.venv/bin/python` (pytest 9.1.1), with `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH="$PWD/src:$PWD/tests:$PWD/tests/acceptance"`, and `TMPDIR="$PWD/order-out/.verify-tmp"`. No packages were installed. All generated test homes, histories and worktrees stayed under order-out/.verify-tmp; they were removed after verification.

Related baseline invocation (136 passed, 6 pre-existing failures):

```sh
/Users/apple/Dev/foragent/.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp="$PWD/order-out/.verify-tmp/related" tests/acceptance/test_order_perspectives.py tests/acceptance/test_orch_refresh.py tests/acceptance/test_lane_dream.py tests/acceptance/test_lane_memory_writers.py tests/test_orch_refresh_unit.py tests/test_orch_dream_unit.py tests/test_order.py::test_perspectives_decisions_cut_keeps_titles_and_latest_twenty
```

Final draft invocation (9 feature assertions failed, 1 fallback passed):

```sh
/Users/apple/Dev/foragent/.venv/bin/python -m pytest -q --noconftest -p conftest -p no:cacheprovider --basetemp="$PWD/order-out/.verify-tmp/new-worker" order-out/tests/acceptance/test_rules_readers_main.py order-out/tests/acceptance/test_rules_readers_order.py order-out/tests/acceptance/test_rules_readers_orch.py --tb=short
```

The explicit conftest plugin supplies the existing offline fixtures to draft modules outside tests/. Published modules use normal conftest discovery. `fa order publish --help` was read. The real `read_plan` / `validate_plan` schema checks and `publish(..., dry_run=True)` with an empty FakeTracker passed offline; no CLI publication or preread was attempted. Additional assertions checked actual lane eligibility, three size-s issues with no dependencies, each exact Goal ending, locked module headers, Python AST syntax and verbatim embedded source equality. `git diff --exit-code -- src tests skills README.md .foragent.yml` confirmed that implementation and existing tests were unchanged.

## Assumptions and open questions

- The supplied path-only refresh/dream prompts gain selected-document text to satisfy the explicit marker requirement; this is the only extra prompt behaviour beyond switching authority paths.
- Missing means no file, not empty content. For fa-order in adopted projects, absence of rules.md preserves decision-history recording; creating/backfilling rules belongs to the docs role.
- Use implicit main for paths without an eligible lane rather than changing lane ownership. No code dependency on the future docs split is needed because of fallback and temporary test documents.
- Open questions: none requiring Jin. The docs role's D99/D100 choice and sequencing are outside r63 point (4).
