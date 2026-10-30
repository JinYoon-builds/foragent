# Refactoring

Source: adapted from `pstack/playbooks/refactoring.md` (pstack by Lauren Tan, MIT License).

The refactoring procedure, architect proposals and "Splitting a module" now live in
[skills/fa-architect/SKILL.md](../../fa-architect/SKILL.md). This page stays so old links work.

In short: reproduce the current behavior and record it as the baseline; write a failing
characterization test first, before you change the code; rule out hypotheses about callers with
evidence and do not guess; when a dependency is unclear, add logging and observe the runtime values;
plan the shape from that evidence; confirm on the same surface as the baseline; report the result
with the before-and-after output.
