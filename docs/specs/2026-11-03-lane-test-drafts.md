# Lane test drafts for decided issues

Jin, 2026-11-03: acceptance tests for a decided issue are drafted by the agent of the lane
that owns it, in a session separate from implementation. This order covers items 1 and 4 of
that decision. The order lane's red check (`fa order red PLAN.json`) is a separate issue, and
implementation runs already cannot edit locked tests (item 3).

## Result

- An open, unapproved issue whose Done when still holds the placeholder
  (`false  # placeholder: acceptance tests are written before this issue can be approved`)
  gets one drafting session from the gate. Nobody has to ask for it.
- The session runs the issue's own lane agent. It writes draft acceptance tests and a plan.
  It changes no source code: the checkout, its branch and origin stay as they were, no PR
  opens, and the issue's body and labels stay as they were.
- The draft is kept outside the source tree, where the orchestrator can read it later.
- Each placeholder issue gets one session. Another tick does not start a second one while
  the first runs, or after it finishes. Issues with real Done when and closed issues get none.
- Several lanes draft at the same time.
- Drafting never holds a lane's implementation queue. A ready issue in the same lane starts
  while that lane's draft runs. When only one slot is free, ready implementation work gets it,
  and the draft starts once a slot is free.

## Contracts

These are the seams the locked tests use. Everything else (where drafts are kept, the
worker command's name, how sessions are recorded, how capacity counts them) is the lane's
choice.

- The gate starts a drafting session through its launcher: the one `fa gate tick` passes to
  the tick (`foragent.launcher.spawn_runner`). The session runs as its own worker process
  (`python -m foragent.cli gate ...`). An approved issue's implementation start keeps its
  existing worker command, `gate run-issue N`.
- The drafting agent writes its draft as files in the directory it runs in: tests under the
  acceptance directory and the plan as `plan.json` at the top. The gate keeps those files
  outside the checkout.
- A Claude lane agent runs with the lane's agent, as an implementation run does, and its
  prompt carries the issue's goal.

## Out of scope

- The orchestrator checking drafts and replacing the placeholder with real Done when.
- `fa order red` (order lane).
- Drafting again after an issue's body changes, and issues in the implicit main lane.
- Showing drafts in the view or status.
- Changing how run capacity is sized (#1635).

## Prototype evidence

A scratch prototype (`prototype.patch`, about 126 source lines in four files) passed the
four new locks and the related scheduler locks. It was removed; only the patch is kept.
