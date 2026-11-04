# Waiting on a person (from #1469)

## Why
On 2026-11-01 the PM said "things are running fine" while the orchestrator waited on a
person. No measured fact showed that a person was being waited on, so no role signal
could call the architect. #1469 dropped this alarm after six review rounds: its source
was never defined. This issue defines one source.

## Source (one)
Observed issue state, written only by the gate's issue listing:
- which issues wait: open issues whose current observed labels carry `needs-human`;
- since when: the observation history of that issue — the start of its latest unbroken
  stretch of observations that showed it open with `needs-human`.

When that history no longer holds the start (it is trimmed), the age counts from the
earliest observation still kept, so it is a lower bound. Age is never a reason to call.

NOTE (out of scope): inbox questions, needs-human marker comments, the orch events journal,
`waiting-person` parks, and a session stuck on a permission prompt. They are not this
source. The 2026-11-01 permission-prompt case is not covered by this fact.

## Public contract
- `fa orch facts --json`: kind `waiting_on_person`, present when one or more issues wait.
  Its `kind_numbers` entry holds `waiting` (count), `oldest_issue` ("#N") and
  `oldest_waiting_seconds`. The human output names the oldest issue and the count.
- A role signal with `kinds: [waiting_on_person]` raises an inbox request like any kind.
- After an answer (`called` or `not-now`) it raises again only when more items wait than
  at that raise, or the oldest waiting item is a different issue. The same items growing
  older, fewer items, or a gate restart stay quiet. A signal that cleared (nobody waiting
  on a tick) is raised again by the existing cleared rule.

## Not decided here
Whether this repository's architect role lists the kind (a role config change after
merge) and the derived default signals.
