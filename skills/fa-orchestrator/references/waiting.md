# Wait-resume notices

A gate request with `wait_resume_key` carries the issue's authored `## Waiting`
section and the native prerequisites observed closed. Deliver that context to every
responsible role named in that section, including docs, through this shared inbox. Re-read
the issue and prerequisites, judge the next condition, then update or clear the
issue's `## Waiting` section through the order tools and mark the request done.

The notice records delivery, not approval or a new work fact. Keep an unapproved
issue unapproved until its authorized approval. Native closure does not interpret
any additional prose condition: choice-free fixes follow clean preread and the
existing publication path; D71 choices and real questions go through planning to
the person. The gate does not interpret free-text conditions without native edges.

## Waiting on an existing issue

When work waits on an existing issue, approve it now with that existing issue in blocked_by;
do not hold it back for a person to approve after the blocker merges. The gate keeps it waiting
while the blocker is open and starts it on the first pass after the blocker's Done when passes
on main, whatever the close reason says.

A gate request with `blocker_key` names a blocker in one of two states. When an issue's Done
when already passes on main while it is still open, close it as delivered without asking a
person; the gate then starts the issues that wait on it. When a blocker closed without
delivering (its Done when still fails on main), the waiting issue does not start: re-point its
blocked_by to the issue that will deliver the work, or remove the blocker, through the order
tools, then mark the request done.
