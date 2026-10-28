# draft, not posted

(GIF goes at the top.)

I stopped babysitting Claude Code between tasks.

It's already good at finishing one task. The tiring part was everything after: "done? run the checks. review it. merge it. now the next one."

Foragent does that loop for me:
task ends → done-when checks → adversarial review → merge → next issue

I describe what to build once, approve the split, and walk away. Lanes, each owning one area of the code, work in parallel in their own worktrees and remember what they learned.

It built itself: 208 merged PRs in 10 days.

Honest limits: needs Claude Code, the background gate is macOS only for now, no native Windows yet.

https://github.com/JinYoon-builds/foragent
