# Show expected change size during preread

## Decision and outcome

Jin decided, through the PM, that a worker's preread returns its expected change size
beside its design: source lines, test lines, and changed files. The orderer or PM sees
when that expectation is above the issue's size label, with a suggestion to split.
They can still publish and approve. The signal informs their judgment.

After merge, the issue's retained comparison shows both expected and real PR size.
A public command exposes those records and the share of estimates that differed.
Missing estimates remain unknown; zero is a known estimate only when explicitly given.
Changing a threshold setting changes the signal. Earlier clean prereads remain usable
when their content has not changed; adding this optional reporting envelope must not
retire them.

## Acceptance outcomes

- Preread shows all three expected counts beside the worker's design.
- Exceeding any dimension's limit for the issue label shows a split suggestion.
  Publication without approval and publication with approval both still succeed.
- A worker answer without an estimate preserves today's question/approval behavior.
  Its size reads as unknown, never as a zero-sized change.
- A repository setting changes the numerical limit. A setting-only edit keeps the
  earlier clean preread usable without another worker turn.
- Each merged PR retains its issue and PR identities, expected counts and measured
  counts. Real size includes deleted lines and every changed file.
- A public read reports the share of wrong estimates and the comparison denominator.
  Unknown estimates stay visible and are excluded from that denominator. Repeated
  reads do not count a merge twice.
- Clean-main preread evidence captured before this feature still permits approval
  without rereading. Existing changed-content and question checks continue to apply.

## Counting assumptions

For this draft, a line count is additions plus deletions of text under `src/` or
`tests/`; every changed path contributes to file count. A suggestion appears when any
count is strictly above its label's limit. Wrong means any expected count differs
from the measured count, in either direction. The denominator includes only complete
known comparisons; an empty denominator reads as unknown. These are transparent
counting conventions, not a tolerance policy or an agent judgment made by a script.

The lane selects initial limits from measured merged PRs and keeps them configurable.
REPORT.md records the local evidence and this prototype's nearest-rank p90 choices:
S 199/193/10, M 524/437/17, L 1579/782/25 (source/test/files).
The locked checks use changed public settings rather than pinning those defaults.

## Public surfaces exercised by this draft

The prototype documents a worker reply `SIZE: source=<n> tests=<n> files=<n>`,
optional `expected_size` counts beside the existing design in preread JSON, and
`size_limits` in repository YAML. `fa order sizes --json` exposes merged comparison
records and their rate. These are public wire, setting and CLI surfaces exercised
by the locks. Internal modules, helper names, persistence layout and key construction
remain the lane's choices. Historical evidence is opaque fixture input; locks only
assert that the public approval command accepts it.

## Scope and ownership

One issue, lane `order`, size `s`, noninteractive. The entry points, preread data,
configuration, and planning guidance belong to the configured order lane. Two small
merge-path connections complete the same expected-to-actual record; they are not
separately useful deliverables. The measured prototype stays within the S limits.
No dependency on another issue or prerequisite patch is needed on main 8b4c713c.

## Out of scope

Automatic redesign requests and any change to retry instructions. No refusal,
publication block, approval block, forced split, or new message to the orderer.

## Proof and delivery

Locked tests exercise CLI results through existing offline Git, tracker and executor
adapters. They do not import size implementation helpers or assert internal names.
Done-when commands run one test file at a time with normal project tooling.
The scratch implementation is provided only as prototype.patch. Final working-tree
changes are the four draft artifacts; source and existing locked test files stay unchanged.
See REPORT.md for node IDs, baseline/prototype results, measurement rows, and gaps.
