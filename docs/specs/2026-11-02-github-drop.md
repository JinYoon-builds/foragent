# Resume work after a brief GitHub outage

## Goal

When GitHub briefly disconnects or returns a server error during execution,
review or CI publication, the run waits and continues automatically. It keeps
completed work, its remaining attempts and an applicable PASS. Nobody needs to
remove a running label, retry the issue or request another review.

## Evidence and cause

The request reports 4 affected runs out of 703 over seven days: #114, #991,
#1105 and #1597, in reviewing or CI. On 2026-11-02 a four-minute outage led
through error, a leftover running label, crash recovery, retries-exceeded and
blocked, then a manual review. These are supplied measurements; this draft
does not claim to have remeasured production.

The current runner admits only rate-limit errors to its existing GitHub wait.
Other tracker errors enter ordinary failure handling, which may itself need
the unavailable tracker. Review has the same gap before and after admission.
The tick already knows how to restart a waiting run and repair its labels.
Starting the ordinary implementation path again can also repeat a completed
review: a prior verdict needs enough evidence to identify the work it judged.

## Required behavior

- A GitHub connection failure or HTTP 5xx that escapes an operation ends that
  invocation in the existing recoverable wait. Cover both the issue-run and
  review-only public entry points, including review admission and cleanup.
- The wait is durable across reopening the local store. A tick before its due
  time leaves the run alone. A later ordinary tick continues it without human
  input. Continued outages can wait again; waiting spends no implementation
  attempts or crash-restart allowance and creates no blocker or human flag.
- The wait record and log preserve the actual outage diagnostic and the due
  time. A connection or server error must not be called a GitHub rate limit.
- When the same approved PR head already passed review, return that head to its
  remaining checks, CI and publication without another implementer or reviewer
  turn. Preserve the current publication and merge checks. A changed head or
  changed approved issue body cannot inherit the earlier PASS.
- HTTP 422 validation errors keep today's failure behavior. Other permanent
  errors do not become waits merely because they came from the tracker.
- CI evidence reads that currently return a `CI evidence unavailable` notice
  continue returning it. The existing rate-limit classifier keeps its narrow
  meaning and all existing readers keep their behavior.

## Scope and constraints

One noninteractive issue, size `s`, lane `orch`. Reuse the existing persisted
wait and ordinary tick restart path. Add no store, outcome, state-machine state,
queue or independent retry loop. Existing evidence records may retain the
facts needed to associate a completed review with the work it judged. Private
names, representation and implementation belong to the lane (D98).

This is transport/status handling and recovery, not judgment about reviewer
prose. Do not add meaning classifiers, case-specific agent instructions or LLM
decisions inside scripts.

## Out of scope

New scheduling or storage machinery, broad changes to rate-limit readers,
changes to CI evidence fallback, a general retry for all tracker failures,
automatic resolution of real review findings, and changes to normal merge or
approval policy. This draft is local and unpublished.

## Public verification boundaries

The acceptance checks enter through the public issue runner, review-only
runner and scheduler tick, using the existing offline Git/tracker/executor
fixtures. Observe persisted run results, attempts, flags, labels, logs,
published head, merge count and executor calls. Locks must not import a private
helper or require a new private name or state representation.

Prove a four-minute connection outage recovers automatically, exercise HTTP
500/502/503/504 after PASS on both entry paths, and reject PASS reuse after the
  head or approved body changes, including a head changed during recovery. Keep the existing rate-limit, 422, CI-evidence
and review-only publication locks passing. REPORT.md records exact nodes,
commands, the main baseline, prototype size and unproven cases. The scratch
prototype is evidence, not an implementation prescription.
