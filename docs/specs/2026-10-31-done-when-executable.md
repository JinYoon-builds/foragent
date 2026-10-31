# Refuse Done when commands that cannot run here

## Problem and root cause
An order used `python -m pytest` on a machine with only `python3`. This repository runs tests with `uv run pytest`. Every attempt failed at the independent check with command not found. Publication checks the command format and referenced acceptance files, but does not establish whether the first executable exists in the environment used by the gate's checks. The gate removes its own virtual environment from that environment. Rewording the affected order alone leaves the same defect available to future plans.

## Required outcome
Both `fa order publish --dry-run` and real `fa order publish`, including `--approve`, refuse a plan if the first executable of any Done when command cannot resolve in the environment the gate's checks use. Check every command across the plan. Show one diagnostic line per failing command, naming its missing executable. When the repository has a project runner such as uv, suggest the corresponding `uv run ...` form; for `python -m pytest ...`, the suggestion includes `uv run pytest`.

A rejected plan produces no publication writes: no acceptance files, spec, Git publication, issues, or labels. Commands whose first executable resolves keep passing the existing publication checks. Validation establishes executability; it does not run the Done when commands or rewrite the plan.

## Acceptance
Through the real publish CLI with existing offline publication fakes and a controlled PATH containing uv and python3 but no python:
- Dry-run refuses `python -m pytest ...`, names python, and suggests `uv run pytest`.
- Multiple failing commands produce separate diagnostic lines, including repeated use of the same missing executable, so a check confined to python or the first failure is insufficient.
- Dry-run accepts `uv run pytest ...` and other resolving executables. It also accepts python when python is present.
- A plan with a runnable command before the unavailable command is refused with a diagnostic only for the unavailable command.
- Real publish with approval and a clean preread is refused for the same missing executable, with the same actionable information and no publication writes.

## Scope
The original executable-validation issue belongs to the order lane. This order also contains the two small orch issues below. No implementation structure, private names, or helper interfaces are prescribed. Shell programs beyond their first executable, command success, automatic command repair, runner installation, and changes to gate execution are outside this order.

## Locked check and break-it rule
Run `uv run pytest -q tests/acceptance/test_done_when_executable.py` from the repository root. The locked check drives the CLI, reuses the existing publication and clean-preread fakes, and compares files and publication effects. Returning success for missing executables, rejecting all commands, checking only the first command, reporting only the first missing executable, flattening multiple diagnostics onto one line, omitting the runner suggestion, or writing before refusal must break the relevant assertion. The baseline main run and its failure lines are reported with this draft; no product implementation is included.

## QA classification
Both QA items are tool defects, not missing agent judgment. Running records and process observations are already available, but status and stop consume different definitions. Authentication failures are transport facts, but they reach the runner's terminal failure path without a bounded recovery opportunity. No new meaning classifier, model decision, agent step order, credential, or permission is needed.

## stop-sees-running
Incident: on 2026-10-31 at 12:02 KST, `fa orch stop N --keep-work` said not running for #1062 and other runs shown as implementing by `fa gate status`, defeating an emergency halt.

The status command's active list retains unfinished running records, including their phase, even after the recorded workers disappear. Stop separately requires process liveness and can refuse that same record. The required result is one definition of running, based on the same facts, across both public commands. An unfinished implementing record displayed by status remains stoppable with its work kept, including when its recorded processes have gone. Do not obtain agreement by hiding unfinished records. A truly finished record remains absent from the running list and is refused without changes. Existing process ownership protections still apply.

The locked CLI test reuses the existing offline work-preservation and process fakes. It reads status before stop, stops the displayed record, verifies the unfinished files on the saved issue branch and offline origin, verifies that owned workers are gone and unrelated workers survive, and reads status again. It also checks a finished record's refusal and unchanged work/state. This current requirement supersedes older expectations that an unfinished record with dead workers must be refused as not running; it does not authorize signaling a different process that reused a PID.

Break-it: refusing a displayed record, hiding it before stop, returning success without stopping or keeping the work, leaving workers alive, duplicating stop records, or changing a finished run must fail the locked check. Run `uv run pytest -q tests/acceptance/test_stop_sees_running.py` only.

## auth-failure-retries
Incidents: three runs or ticks on 2026-10-30/31 became blocked after transient SSH authentication failures that succeeded on retry. Git transport exceptions are currently converted to ordinary runner errors; existing fetch recovery handles a different ref-update failure, and GitHub recovery handles rate limits rather than authentication. The authentication failure reaches final blocking without a brief bounded retry period.

When a run encounters a Git or GitHub authentication failure during a read or write, wait briefly and retry a bounded number of times. A one-off failure such as `Permission denied (publickey)`, `Could not read from remote repository`, or a GitHub HTTP 401 must let the same run continue once transport succeeds. It must not block the issue, repeat implementation work, or duplicate successful publication effects. If the transport keeps failing, stop retrying and retain one clear line naming the authentication failure in the run's reported reason, without marking the issue blocked or needs-human. Non-authentication failures keep their existing behavior.

The locked runner test uses the existing offline runner, Git publication, and tracker fakes. Injected transport failures cover Git reads and writes, plus GitHub reads and writes through the real adapter and an offline transport. Available-transport controls must complete. One failed attempt followed by success must wait and complete the run once. Persistent failures must retry with waits, terminate within a finite budget, report a single-line reason, and create no PR. A non-authentication Git failure must retain its existing failure behavior. No real remote, commit, push, or signal is used.

The implementation owns its retry count and delays. The acceptance harness uses generous watchdog ceilings of 20 observed transport attempts and 60 seconds of requested recovery waits; these detect runaway recovery rather than choosing exact production settings. Credentials, authentication configuration, permission expansion, automatic issue restarts, and unrelated error recovery are outside this order.

Break-it: immediately blocking on the first authentication failure, skipping the wait, retrying forever, failing to continue after recovery, repeating implementation or publication effects, losing the failure reason, or treating an unrelated Git error as an authentication retry must fail the relevant assertion. Run `uv run pytest -q tests/acceptance/test_auth_failure_retries.py` only.

## Delivery constraints
This draft contains three independent small issues, one in order and two in orch. The orch lane owns the runner behavior and serializes its two issues; there is no new cross-issue contract or dependency. This planning run changes only the output plan and locked test artifacts. It implements none of the fixes. Each issue embeds its complete locked test under acceptance_files, uses the repository runner, and has no statements field.
