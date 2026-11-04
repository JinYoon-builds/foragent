---
name: order-after-11-03
description: Work order set 11-03 (Jin left it to the PM): merge owner, then 2-3 days of structural cleanup, then remaining features; no new internal-mechanism feature issues meanwhile
metadata:
  type: project
---

Order from 2026-11-03 (Jin left it to the PM): (1) merge owner from 11-04 (#1502, see [[merge-owner-before-launch]]); (2) 2-3 days of structural cleanup, the same as the 11-01 "subtraction period": gather scattered rules into one place, make tests check results through public entry points, delete unused paths, with number targets; (3) remaining features.

**Why:** most 11-03 defects came from several writers and scattered rules; adding internal mechanisms now adds more of the same.

**How to apply:** do not approve new feature issues that add internal mechanisms (#1499, #1500, #1501, #1745 and similar); finish what is already running; bug fixes the PM approves and first-impression work (install, README, first run) go on alongside. Near the end of the merge-owner work, call the architect for three lists with numbers: code doing the same job in several places, shared resources and their owners, tests that check internals; the cleanup scope is set from those.

Decision 2026-11-04 (Jin via PM, r408): when an issue merges, its locked tests unlock and become that lane's ordinary tests; the lane tidies them in dream (merge same-feature tests, delete tests of removed features, turn internal-looking tests into behaviour tests). Guards: delete a test that passed on main only when another test checks the same behaviour; the reviewer checks with the break-it rule; record test count and total time before and after. Build it in the cleanup period after the merge owner; pick the first targets with the architect's "tests that check internals" numbers.
