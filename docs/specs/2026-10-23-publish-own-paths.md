# fa order publish commits only its own paths on the latest main

Evidence (2026-10-23): the planning session and the orchestrator share one main checkout. A half-written acceptance test draft from one session sat untracked in main while the other published; and `fa order publish` failed with "non-fast-forward" whenever main had moved, so the orchestrator rebased by hand and retried.

- `fa order publish` commits only the paths it wrote (its spec and the plan's acceptance files); other untracked or modified files stay out of the commit and unchanged.
- It takes a lock, fetches, places its commit on the latest main and pushes, so a moved main never fails a publish; concurrent publishes in one checkout both land.
- skills/fa-order drafts acceptance tests and specs in a temporary folder outside the main checkout and lets publish place them. The folder layout stays as it is.
