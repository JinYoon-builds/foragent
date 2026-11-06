# Design contract

These are product-wide principles for every screen a person sees in Foragent,
starting with the dashboard (the lane view). They come from D112 in
`docs/decisions.md` and the 10-30 r53 main screen (`docs/specs/2026-11-04-r53.md`).

View-lane issues and reviewers read this file before they change a screen.
Lane memory may point to this file, but lane memory is not the source of these rules.

## Rules

1. One screen shows one piece of information. The main screen shows only what is
   happening now; everything else goes somewhere else.
2. Details sit behind the acorn click: facts about one issue (why it waits, the next
   condition, review, CI, a stalled warning, the issue before it) appear in the bubble
   that opens when a person clicks that acorn. The GitHub link is one line inside the bubble.
3. The main screen keeps the r53 layout: the big squirrel with its bubble summary, the
   progress bar, one line per role, and the lanes (squirrels with acorns, the phase name
   inside each acorn). On a narrow screen the big squirrel stacks above the bubble and is
   never dropped.
4. The person-facing view uses Korean only.
5. No person's name appears on screen.

Adding anything to the main screen is a matter of taste and needs a person's approval.
The default place for new information is the bubble.
