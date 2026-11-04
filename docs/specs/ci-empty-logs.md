# CI retries carry failed-job evidence before workflow completion

## Source and confirmed cause

Drafted against 657bef0872e91442d760394b9e46adbf920174a1, the worktree HEAD and latest locally available origin/main. Read-only git ls-remote origin refs/heads/main failed because github.com could not be resolved. Remote freshness is unverified; revalidate against remote main before later publication. No fetch, branch switch, commit, push, reset, checkout or stash was used.

The reported cause was confirmed with injected GitHub responses before writing the draft. Real GhTracker.pr_checks returned fail for test (1)=fail plus acceptance=pending. The same head's workflow had status=in_progress and conclusion=null, with a completed failed job and readable annotation/log responses. Real pr_failed_logs returned an empty CIEvidence, not_started=false, without requesting jobs, annotations or logs.

At tracker.py:1387-1391, any ordinary failing bucket wins over pending. At tracker.py:1542, evidence collection skips every workflow without a failed run conclusion. In .github/workflows/ci.yml, fail-fast is false. These facts make a failed job inside an unfinished workflow a legitimate state that the evidence collector drops. run_publish_ci.py:33-34 then builds CIFailure from the empty result; run_attempt.py:63 produces CI failed: followed by no evidence. A blank-text guard alone would leave the missing job evidence unfixed.

There is a second transport constraint, not a different incident cause: current GitHub CLI run view refuses logs while the workflow is in progress, even when a selected job is already completed. Merely widening the workflow filter while keeping a run-wide log request cannot deliver the failing tests. The public GitHub job log API addresses individual jobs. These are facts for the lane's transport choice, not a prescribed implementation sequence. Sources: https://github.com/cli/cli/blob/trunk/pkg/cmd/run/view/view.go (log status checks) and https://docs.github.com/en/rest/actions/workflow-jobs#download-job-logs-for-a-workflow-run.

## Outcome and timing decision

When ordinary PR CI is reported failed, the next implementation retry receives the failed job names, their readable annotations, and the failing test identifiers and other diagnostic text from their bounded log tails. A completed failed job is evidence even while its workflow is still in progress and sibling jobs are pending. Read that evidence now: do not wait for the whole workflow, add a polling delay, or spend an evidence-only implementation retry. Preserve useful tails from every failed job rather than letting another job's output hide them.

If a requested evidence component cannot be read, or GitHub reports a failure but supplies no matching run/job evidence, the retry prompt explicitly says that evidence is unavailable and retains every readable component. The locked English availability notice contains the word "unavailable"; the full wording and internal representation belong to the lane. Non-rate-limit read errors and empty log responses are missing evidence, not silent blanks or fabricated test failures. Preserve existing GitHub rate-limit/wait handling.

Completed workflows retain today's feedback and retry budget. Preserve the existing not_started rule: only failures established to have never started produce the ci-not-started hold without another worker call or consumed repair attempt. Pending siblings do not erase that evidence, and unavailable metadata alone does not prove that a job never started. Existing API-verified acceptance workflow exclusions remain; a merely acceptance-named ordinary job is still ordinary CI. Future lost-test IDs printed in a failing step must survive this same log-tail path as raw diagnostic text. This order does not change shard verdicts or exclusions. The implementing lane chooses the transport, structure and error representation; semantic diagnosis and repair remain the worker's judgment.

Reading the already failed job now is the chosen behavior. Waiting for run completion would extend today's immediate failure path by the slowest pending sibling, which can be an acceptance shard. There is no benefit in withholding already completed failure evidence. The acceptance fake keeps siblings pending until the worker repairs the change and checks that the runner introduces no sleeps on the known-failure path. Existing bounded feedback and full-text cut behavior remain available; no arbitrary wall-clock performance threshold is introduced.

This is missing factual context for the worker: the harness discards already available GitHub evidence. The repair gives the worker that context. It does not teach a script how to interpret a failure or decide which code to change. Existing not_started classification is retained, not expanded.

## Scope and ownership

One issue, key ci-empty-logs, lane board, size s, no dependencies. .foragent.yml assigns tracker.py to board. The shared CI feedback formatting boundary has no dedicated lane and belongs in this same root-cause repair; no lane or role is created. Fixed lane skills remain as configured; issue-specific tool/review skills are for the lane to choose.

Files:
- src/foragent/tracker.py
- src/foragent/run_publish_ci.py
- tests/test_tracker.py
- tests/acceptance/test_ci_empty_logs.py

The file list covers the evidence collector, its retry-text boundary, focused unit coverage, and the new locked acceptance file. The draft itself changes none of those production or existing test files. Nonlocked tracker unit expectations about ordinary evidence-read failures may need to follow the new explicit-unavailability outcome; existing locked assertions stay unchanged.

Out of scope:
Changing pr_checks failure timing, CI polling budgets, fail-fast, retries, acceptance measurement/shard verdicts, the trusted acceptance workflow exclusion policy, not_started classification, rate-limit behavior, agent instructions or repair judgment; implementing drafts/shard-red; adding text classifiers, keyword/regex judgment rules, model decisions in scripts or a required agent step order; publishing, committing, or changing existing locked tests.

## Locked verification

The complete acceptance file is embedded in this issue's acceptance_files. It calls public run_issue using the real GhTracker readers with fake GitHub command responses and the established offline runner/executor/history fixtures. Tracker writes are in-memory. No CI selection, not_started, evidence assembly or prompt helper is replaced. Temporary Git histories use the repository's forbidden-command guard.

Three test functions produce seven cases. The first pairs a completed-workflow control with an unfinished-workflow failure and checks two failed jobs, both annotations, multiple failing test IDs, a raw lost-ID log tail, exclusion of an API-verified measurement run, bounded feedback and prompt delivery without a wait. The ordinary second failure is named acceptance (0) to prevent display-name-based exclusions. The fake rejects evidence reads of the trusted measurement run and refuses run-wide logs while the ordinary run is unfinished.

Five missing-evidence cases cover a denied job log, an empty job log, denied annotations with readable test logs, denied job metadata, and no matching runs despite failed PR checks. They require explicit unavailable text and retain independently readable evidence. The last test pairs today's completed not_started hold with the same failure plus pending siblings; both must hold on attempt one without another worker call or any log retrieval.

The synthetic lost-ID payload checks generic text delivery only. It does not change which acceptance workflow is counted or implement shard-red. Excluded trusted measurement workflows remain excluded even if they later turn red for lost tests.

Before the new lock ran, the unchanged locked ci-feedback and ci-not-started modules passed all 13 cases at this base. The two named tracker exclusion/identity controls passed all 28 cases. New locks are required to fail against this base for missing failure evidence/hold behavior and pass after implementation. No full suite appears in Done when and no post-implementation pass is claimed.

Done when:
```sh
uv run pytest -q tests/acceptance/test_ci_empty_logs.py
```
```sh
uv run pytest -q tests/acceptance/test_ci_feedback.py tests/acceptance/test_ci_not_started.py
```
```sh
uv run pytest -q tests/test_tracker.py::test_ordinary_ci_excludes_acceptance_jobs_reports_and_failed_logs tests/test_tracker.py::test_commit_status_cannot_impersonate_acceptance_job
```

## Local incident count

At 2026-11-01 20:53:02 +09:00, the readable retained logs under ~/.local/share/foragent/JinYoon-builds__foragent/logs contained 46 standalone runner CI failure records, of which 29 had an empty body, across 24 issues, in the preceding seven-day window (from 2026-10-25 20:53:02 +09:00). Twenty-two empty entries were immediately followed by worker session records; seven were terminal attempt-3 records followed by later run output. Count the 29 failure records rather than double-counting copies inside executor JSON, reviews or stdout. The count describes retained local evidence, not deleted logs or a global incident total. All 119 authoritative issue-N.log files were created within this window; the oldest file creation was 2026-10-29 04:59:18 +09:00. Runner markers themselves have no per-line timestamp. The local accompanying DIAGNOSIS.md records the method and each blank entry; implementation does not depend on that file or on access to these private logs.
