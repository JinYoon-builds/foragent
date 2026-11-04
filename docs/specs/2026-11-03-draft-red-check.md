# Check that a lane's test draft is red on clean main

## Why
Jin decided on 2026-11-03 that tests for a decided issue (Done when still `false  # placeholder: ...`)
are drafted by the owning lane's agent in a session apart from implementation, and that the
orchestrator checks each draft before preread: the new tests must fail on clean main for a real
reason. A missing module or a collection error does not count. On 2026-11-03, 21 open issues
still carry the placeholder.

Today this check is done by hand. `fa order publish` and `fa order preread` already refuse acceptance
files that fail collection on the base, but nothing tells whether the draft's tests actually fail
there, or fail only because a module is missing at run time, or already pass. The reviewer in
`fa order prepare` reports red runs as its own reading; it covers statement issues only.

## Result
One `fa order` command takes a draft plan file and answers whether the draft is red:

- It runs the draft's acceptance files against the base branch as fetched (not the caller's
  working tree, which may hold uncommitted work), with the project's own test command.
- The draft is red only when every collected test in every acceptance file fails there by a real
  failure. These are not red, and the output names the test, file or module:
  a test that already passes (or is skipped), a file that collects no test, a collection or setup
  error, a failure caused by a missing module.
- Exit 0 when red, non-zero otherwise.
- It changes nothing: no checkout file, index, branch, GitHub write or model run.

Command contract used by the orchestrator and the tests: `fa order red PLAN.json [--repo-root DIR]`.
How it runs, what it prints beyond the named items, and any extra flags are the lane's choice.

## Out of scope
- Starting lane drafting sessions, running several lanes' drafts at once, and keeping drafting off
  the implementation queue (orchestrator scheduling, orch lane).
- The measured numbers: how many decided issues wait for tests and the time from decision to
  approval (orchestrator facts, roles lane).
- Changing preread, approval, publish or the locked-test rule.
- Non-pytest test commands: the command may refuse them.
