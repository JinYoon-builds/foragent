# GitHub writes go through FA commands

## Outcome and root cause

[person, 2026-10-31] Jin had to type four operations by hand: close superseded issue #924, close stale dream PRs #898/#901/#940, add an allow rule, and move lane labels after splitting a lane. Agents tried bare gh writes, Claude Code refused them at its permission boundary, and the work returned to the person. Open-source users face the same gap. These incident details are supplied by the person; no live GitHub lookup was performed.

[code, baseline main 459331cf0e285b1be7f44f2dc7db6d1adfa5d827] The tracker already closes PRs, but the public orchestrator CLI has no dream redo action. The gate loads changed lane paths but continues to route issues by existing lane labels. Setup offers other optional integrations but no project FA command grant. Current skills already use many FA commands and retain read-only gh examples; the measured guidance gap is the missing explicit FA write boundary and missing-capability proposal route, not a verified list of current bare-gh write examples.

The root fix supplies the missing command, keeps computed lane ownership with its one gate writer, makes permission consent available at setup, and gives agents the relevant command boundary and fallback context. Scripts do not judge whether work is obsolete, which prose means a lane, or which unimplemented capability ought to exist. This order adds no runtime intent matcher, similarity threshold, case-specific incident branch, LLM decision inside a script, or imposed agent workflow.


## QA addition — 2026-10-31

[person, 2026-10-31] Moving the split lanes failed because GitHub labels `lane:review-engine` and `lane:roles` did not exist, and the board Lane choices were still `main, adopt, order, bench, orch, view, board, skills, design`.

Classification: this is a tool defect, not an agent judgment problem. Configured lanes were not projected into the required GitHub label and board resources. The earlier acceptance fake also allowed assigning an unprovisioned lane label, so it did not expose that missing prerequisite. The existing board API can extend choices while preserving their identities; the missing behavior is automatic convergence from current lane configuration through the public gate tick. No semantic classifier, agent step order or new agent restriction is added. Provisioning before applying a label/card value is the deterministic tool contract, not a forced sequence for agent judgment.

The locked gate test now starts with an existing board and missing new-lane resources, adds `review-engine` or `roles` to the config, and calls the public tick. It requires the GitHub label and board Lane option to exist on the next tick, the matching issue and card to move, approval/body/unrelated state to survive, and a second tick to attempt no writes. The fake rejects lane-label application before the label and configured board option exist. Remote board state is read through public GraphQL, not only a local cache. Both reported names are ordinary test parameters, not production special cases.

The text-check gap is also a tool/test coverage defect. Board project writes belong to the same existing FA-only write boundary. The three locked text checkers explicitly reject `gh project item-edit`, `gh project field-create`, `gh project item-add`, and `gh label create|edit|delete`; read-only project and label inspection remains allowed. Label verbs were already in the original pattern; targeted negative controls verify them alongside the newly covered project verbs. These are acceptance text checks and do not ship a runtime command classifier.

## Settled behavior

1. The next gate tick after a lane-path configuration change or a new lane first creates any missing configured lane labels and any missing Lane options on the configured board, then aligns each open issue's lane label and board Lane value from its explicit Files and the configured paths. It removes obsolete lane labels from issues while preserving the body, approval, unrelated labels, existing board option identities and unrelated card values. A running issue keeps both its label and board Lane value until its run ends; the pending change must not be forgotten. Closed issues are unchanged. The gate is the one owner of this reconciliation. A second tick with the same converged configuration attempts no more tracker or board mutations, including label/option creation or redundant rewrites. Existing behavior for ambiguous or unmatched paths is retained. Reuse the configured board; this change does not create a new board project or remove old labels/options.
2. The public recovery command for this order is `fa orch dream --redo <lane>`. It closes only the requested lane's stale open dream PR, retains the lane notebook (including pending lessons), and reaches a fresh dream through the ordinary gate/worker admission path. By the next eligible tick the fresh dream starts once. No generic gh passthrough or arbitrary PR-close command is required.
3. `fa init` proposes one project-scoped allow rule, `Bash(fa:*)`, once. Explicit acceptance adds exactly one rule; decline grants nothing. Both answers are remembered for this project. Other settings survive. Either project Claude settings file is acceptable; storage details belong to the lane. A dry run neither asks nor records consent. Install docs and setup guidance explain the offer. Install docs state that FA commands do not expose permanent deletion, money transactions, sending to others, or permission widening. This narrowly consented local setup edit is not a general permission-expansion command. Existing approval gates still apply.
4. Orchestrator/planning guidance and role/lane agent text route GitHub writes through FA commands. Without a fitting command, the agent proposes an issue for one. Remove bare gh write instructions; keep read-only gh inspection and discovery. Update the shipped source and generated text, while downstream user-owned definitions remain user-owned.

## Today's hand-typed actions: essential coverage only

| Action | Included or left out | Reason |
| --- | --- | --- |
| Close superseded issue #924 | Left out of new command work | Already covered by the approved order-replaces decision and `docs/specs/2026-10-31-order-replaces.md`, with locked checks in `tests/acceptance/test_order_replaces.py`. Its optional `issues[].replaces` contract closes replaced work with approval. This baseline still rejects that contract, so coverage is approved work, not a claim that it is implemented. Do not add a duplicate generic close command. |
| Close stale dream PRs #898/#901/#940 and restart their dreams | Included: dream-redo | Existing worker and tracker capabilities lack one public recovery command. One lane-scoped redo covers all three incidents without hard-coded PR numbers. |
| Add the project allow rule | Included: init-fa-allow | No FA setup offer exists; consent once replaces repeated hand edits. |
| Move lane labels after a lane split | Included: lane-relabel | This is derived state. The gate provisions missing lane labels and board Lane options, then aligns issue labels and card Lane values. A manual relabel or board-edit command would preserve the root cause. |

The three guidance issues make those paths discoverable and prevent an uncovered operation from being silently handed back to the person. No other commands are added just to widen the API.

## Lanes, scope and dependencies

Use existing lanes only. Board owns tracker/board reconciliation; roles owns dreams, orchestrator skill and role guidance; adopt owns init and its installation guidance; order owns planning guidance; skills owns the lane-agent generator and shipped lane definitions. The person's explicit init/adopt and order ownership resolves the overlapping legacy paths in `.foragent.yml`. Unlisted command adapters and role-definition sources follow the owning feature. The three shipped role definitions belong to roles; the remaining shipped agent definitions belong to skills. No two issues in different lanes change the same file.

Every issue is s or m. Each is independently buildable and verifiable: there are no `blocked_by` dependencies and no new lanes or speculative skeleton. The redo spelling is a shared public behavior contract already settled here, so writing its guidance does not depend on an internal implementation interface. Private structure, function names, matching implementation, persistence formats and worker-launch plumbing belong to the implementer.

## Locked acceptance and break-it review

Each child carries its complete acceptance file and a serial `uv run pytest -q tests/acceptance/...` Done when command. Files are staged under `order-out/tests/acceptance/` for this planning-only run; publication paths are under `tests/acceptance/`. No `statements` field is used. Production code and existing tests remain untouched.

- Lane checks call the public gate tick with a fake tracker/board and Git/process/sensor boundaries. New configured lanes must first have their label and board Lane option provisioned, then matching issue labels and remote board Lane values must agree. Existing options and unrelated card values survive, and a second tick attempts no label, option or card mutations. Exact and glob Files move on the very next tick, even before the normal tracker refresh interval. A control issue stays on the old lane; closed work stays unchanged; unapproved open work also moves. Body bytes, approval evidence, unrelated labels and comments remain unchanged. A running issue keeps its lane, then moves after its run. Further ticks, a state reload and a full refresh attempt no new tracker writes. Wrong ownership, skipped relabel, an extra lane label, body edits, approval loss, premature movement, forgotten deferral and duplicate writes all fail.
- Dream checks call the public recovery CLI and gate tick for two different lane names. They prove the stale PR blocks ordinary dreaming, then require only that PR to close once, one fresh launcher call, untouched other PRs and byte-preserved notebooks. A later tick cannot repeat the completed dream. The fake worker records ordinary completion; no real executor, GitHub or Git-history mutation runs.
- Setup checks call the real public init CLI with injected Git/GitHub services. They exercise acceptance and decline, both directly and after a dry run, with unrelated settings in both project settings files. They require one explicit offer, exact grant accounting, no grants or settings rewrites on decline, and no second offer or rewrite on rerun. A text check verifies that install docs expose the offer and its boundaries. Silent grants, skipped grants, broad grants, duplicates and destroyed settings fail.
- Text checks inspect shipped skills and public generated role/lane definitions. They require the positive FA write policy and missing-command issue proposal, reject bare gh write examples (including API, project item/field and label writes), and preserve current read-only PR inspection and skill search. These patterns are acceptance text checks, not shipped judgment rules.

Only targeted files are run, once per final file with `uv run pytest -q`; no pytest-xdist or full suite. Use a pinned main snapshot when main has advanced beyond the supplied checkout; no branch switching or history mutation is needed. Paste the actual baseline failure lines in the report. Run publication dry-run after assembling the plan. Network is blocked, so any live tracker-dependent preview must be reported as blocked; a fake-tracker invocation of the same public CLI can additionally verify the complete local preview without publishing.

## Assumptions

- The example redo spelling and allow rule in Jin's decisions are adopted as this order's public CLI and permission contracts; internal design remains open.
- A fresh dream may be dispatched by the next eligible ordinary gate tick rather than forcing an executor to start inside the command.
- Remember an accepted or declined permission offer per project; the task is for project Claude settings, not account-wide grants.
- Existing ambiguous/unmatched ownership behavior is not redefined. Acceptance uses uniquely owned paths. Board provisioning extends the configured existing project; projects without a configured board still receive lane labels without enabling a board.
- The explicit lane directions in this request take precedence over older overlapping lane globs. The lane-agent template is owned by the existing skills lane.
- No network facts or fresh live issue status are assumed; replacement closure is excluded based on the checked-in approved spec and its locked tests.

## Baseline verification evidence

Executed serially against the supplied checkout at `459331cf0e285b1be7f44f2dc7db6d1adfa5d827`, which equalled main at the start. While this planning run was active, main advanced independently to `37d3acc42ab45a3d6f8f65871b939e88fd75051f`; the only difference is another order spec, and a Git diff verified that production source, existing tests, skills, agent text, project config and tooling are identical. No checkout or history mutation was performed. Each new file fails on missing behavior, without collection or fixture errors. An existing development environment was reused without syncing packages; caches, temporary homes and test state were confined to this working directory.

```text
uv run pytest -q order-out/tests/acceptance/test_fa_lane_relabel.py
E   AssertionError: Next tick must compute the new lane from Files
E   AssertionError: Deferred lane work must survive until run completion
E   AssertionError: Establish convergence before checking idempotence
4 failed in 0.73s
```

```text
uv run pytest -q order-out/tests/acceptance/test_fa_dream_redo.py
E   AssertionError: fa orch dream --redo must be available: usage: fa orch [-h]
2 failed in 0.75s
```

```text
uv run pytest -q order-out/tests/acceptance/test_fa_init_allow.py
E   AssertionError: fa init must propose the explicit Bash(fa:*) allow rule once
E   AssertionError: installation docs: explain the optional FA permission rule
5 failed in 1.20s
```

```text
uv run pytest -q order-out/tests/acceptance/test_fa_roles_github_guidance.py
E   AssertionError: fa-orchestrator: explicitly route GitHub writes through FA commands
E   AssertionError: generated fa-orchestrator: explicitly route GitHub writes through FA commands
E   AssertionError: generated fa-architect: explicitly route GitHub writes through FA commands
E   AssertionError: generated fa-docs: explicitly route GitHub writes through FA commands
4 failed in 0.05s
```

```text
uv run pytest -q order-out/tests/acceptance/test_fa_order_github_guidance.py
E   AssertionError: fa-order: explicitly route GitHub writes through FA commands
1 failed in 0.02s
```

```text
uv run pytest -q order-out/tests/acceptance/test_fa_lane_github_guidance.py
E   AssertionError: generated sample: explicitly route GitHub writes through FA commands
E   AssertionError: generated another-lane: explicitly route GitHub writes through FA commands
2 failed in 0.08s
```

`fa order publish --dry-run order-out/plan.json` succeeded with the public CLI using an injected FakeTracker. Tracker writes, network connections and subprocesses inside the preview were forbidden. This validates the schema and previews six issues and seven publication files (one spec plus six locked tests); it does not verify live GitHub status and does not publish or approve work. Each embedded acceptance file was checked for byte equality with its staged file and parsed as Python.

## QA verification evidence

The four changed acceptance files were each run serially with `uv run pytest -q` against an immutable snapshot of main `0e34caff1e030d037e5e2d3b2901f4cdcd99155f`. The snapshot was materialized inside this working directory with read-only Git archive operations; no checkout, commit, push, reset or stash was used. Tests imported that snapshot's source and inspected its shipped skills/agent text. Only the changed acceptance files were copied into its order-out directory. Source code in the supplied checkout was not modified. The final runs contain 13 expected behavior failures and no collection or fixture errors. Unchanged dream and init files were not rerun for this QA addition; their earlier evidence above is historical.

```text
uv run pytest -q order-out/tests/acceptance/test_fa_lane_relabel.py
E   AssertionError: Next tick must compute the new lane from Files
E   AssertionError: Deferred lane work must survive until run completion
E   AssertionError: Establish convergence before checking idempotence
E   AssertionError: The next tick must create the configured lane's GitHub label
6 failed in 0.88s
```

```text
uv run pytest -q order-out/tests/acceptance/test_fa_roles_github_guidance.py
E   AssertionError: fa-orchestrator: explicitly route GitHub writes through FA commands
E   AssertionError: generated fa-orchestrator: explicitly route GitHub writes through FA commands
E   AssertionError: generated fa-architect: explicitly route GitHub writes through FA commands
E   AssertionError: generated fa-docs: explicitly route GitHub writes through FA commands
4 failed in 0.05s
```

```text
uv run pytest -q order-out/tests/acceptance/test_fa_order_github_guidance.py
E   AssertionError: fa-order: explicitly route GitHub writes through FA commands
1 failed in 0.02s
```

```text
uv run pytest -q order-out/tests/acceptance/test_fa_lane_github_guidance.py
E   AssertionError: generated sample: explicitly route GitHub writes through FA commands
E   AssertionError: generated another-lane: explicitly route GitHub writes through FA commands
2 failed in 0.10s
```

A targeted checker control run (`uv run python` with the same offline environment) exercised each of the three text checkers: all six requested project/label write forms were rejected, and six read-only project/label/PR forms were accepted, for 36/36 controls passed. These controls verify the acceptance checker, not product implementation.

The revised `fa order publish --dry-run order-out/plan.json` was run from the same pinned main snapshot using an injected FakeTracker. Network connections, tracker mutations and subprocess launches inside the preview were blocked. It returned exit 0 and previewed six issues plus the spec and six acceptance files, without publishing, approving or modifying Git history. Embedded acceptance contents were checked byte-for-byte against all six staged files and parsed as Python.
