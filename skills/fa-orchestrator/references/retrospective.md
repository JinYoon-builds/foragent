# Repeat holds and the daily retrospective

## Why count by defect class

A reason code (`retries-exceeded`, `review-dispute`, ...) says where a run stopped, not what was
wrong. The same defect class (for example two writers for one state, or check-then-act) can stop a run
under different reason codes, and one reason code can hide different defects. On 2026-10-27, #370 had
taken seven per-site hints (about 15.5 run-hours), #415 four and #154 three: each hint fixed the last
site the reviewer named, and the next review found another site of the same defect. Escalating every
repeat conflicted with D71 (ask the person only for product decisions), so retry-with-hint had become
the default. A second hold for the same defect class therefore means the issue itself is wrong: its
Goal needs redesign (for example one writer per state, D95) or it needs a split, through an order edit
and re-approval.

## Why a daily retrospective

On 2026-10-27 the #370 loop was found only because a person asked about a merge gap. A short daily
look at `fa orch facts` and `fa orch log` finds lost run time and repeated failures without waiting
for someone to ask.

## The architect question

The four signals are read from `fa orch facts` and the log; there is no threshold automation, and
the call stays the orchestrator's judgment (Jin, 2026-10-27):

- overlap waits repeating on the same hot files;
- repeat holds whose defect class is design (two writers, check-then-act);
- files over their line limit;
- approved work concentrated in one lane while other lanes are idle.

Until `fa orch architect` is available, hand code-design causes on as an order.
