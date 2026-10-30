---
name: team-identity-1003
description: #1003 team-mode identity — by:<login> owner labels, authenticated_login cache, legacy_owner, and test pitfalls found while building them
metadata:
  type: project
---

#1003 (D78 revision, 2026-10-30): owner truth is one `by:<login>` label. Shared rules live in `src/foragent/ownership.py` (`parse_orderer`, `effective_orderer`, `require_owned`); other lanes (orch, view, memory) must call these instead of parsing labels themselves.

- `order.publish` resolves the owner (`_order_owner`) before git and ensures the `by:` label before any file or issue. New issues go through `_create_owned`: it reads the issue back and closes it if GitHub dropped the label. An existing order keeps its parent's owner, so republishing or approving never transfers it.
- `Tracker.authenticated_login(refresh=False)`: GhTracker caches it in `capacity.machine_root()/github-identity.json`, keyed by a hash of GH_HOST, all four token variables and the gh `hosts.yml` text. FakeTracker has a `login` kwarg that defaults to `actor`, so test publications now add `by:<actor>` (for example `by:human`).
- `legacy_owner` is validated in `config._validate`, a wrapper around `config_validation._validate`. Only the memory issue may edit `config_validation.py`.
- `fa init` (`init._identity_notes`) refreshes and prints the account, then lists issues to warn when another person's orders exist without `legacy_owner`. Fake gh runners in `tests/test_init.py` must answer `["api","user"]` and the paginated issues listing.

**Pitfall:** never `from foragent.capacity import machine_root`. An autouse conftest fixture patches `capacity.machine_root` for each test, so a name bound at import keeps the first test's path and the cache leaks between tests. Always call `capacity.machine_root()`.

**Why:** these points cost a debugging round in #1003, and later team-v1 issues build on the same contract.
**How to apply:** use this when touching publication labels, identity, or anything that writes under the machine data root.

Review round 2 (#1003):
- Telling an unconfirmed creation from legacy work needs a positive fact, not a guess from "no label + empty map" (round 4 review). Every parent and child body in its creation request carries `order.OWNED_MARKER` (`<!-- foragent:owner-label -->`). It names no owner. Republication keeps the marker only where it already exists, so legacy bodies never gain it. Unconfirmed = marker + no `by:` + not yet part of the order (parent: map `{}`; child: not in the map). Legacy parents and children are reused, and `legacy_owner` owns them.
- `order._retire` closes first, then retires the markers (`<!-- foragent:` → `<!-- foragent-refused:`). Each step has its own try, so one failure does not skip the other. A retry also closes open issues that carry this order's retired markers.
- Do not create with retired markers and edit them in afterwards. A body edit after creation can fall in the same second as `--approve`, and `scheduler_approval._snapshot` (edit >= approval) then voids the approval (round 3; `test_reapproval_of_preexisting_acceptance_change_passes_gate` caught it).
- Do not re-derive gh's credential choice. gh picks the token variable by host kind (*.ghe.com and github.localhost use GH_TOKEN), and the default host can come from hosts.yml. The cache key therefore hashes GH_HOST, all four token variables, and the full hosts.yml.
