# The merge owner's publication pass reads only what it needs

## Problem
On 2026-11-07 10:12-10:15 each merge-owner publication pass took over two minutes. It read every issue in the
repository (2,400+, open and closed) once for the lock check and again for every publication to find the order's
existing parent. Passes that ran out of the GitHub time budget ended silently, so pending publications sat 15-40
minutes with nothing in merge-owner.log.

## Out of scope
- What the lock check refuses and how a republication finds its parent (behaviour stays the same).
- Other gate passes.

## Also
Four unit test files import locked acceptance modules again after #1951/#1952; the locked import check fails on main.

## Also
fa order flush waits for a merge train while holding the journal and publication locks; on 2026-11-07 one flush held them 27+ minutes.
