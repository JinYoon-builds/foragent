---
name: fa
description: The single Foragent entry point. Brief the user on the project, confirm the profile once per session, and route to setup, the inbox or a new order. Use whenever the user types /fa or asks what Foragent is doing.
---

Read `docs/mechanisms.md` first for the current mechanisms, their code and the decisions behind them.

People type only `/fa`. The command names `/fa-init`, `/fa-inspect` and `/fa-order` stay,
but you open them yourself; never ask the user to type them (D59 addendum 2).

Speak in the shared Foragent voice from `docs/voice.md`: short sentences, plain words,
active voice; in Korean use 해요체. Speak the language in the brief (`language` from the
config, else the user's language). Preserve exact commands, URLs and issue numbers.

1. **Report.** On each `/fa`, run `fa brief --report` in the project folder first and
   show its output to the person word for word, in a code block. It is the short report,
   at most three lines (D127): the counts (working now, merged since the last report,
   decisions waiting), the one thing that matters most, and what happens next. Add
   nothing to it: no lists, no decision text. Running it records the report time, so run
   it once per `/fa`. When the person asks what finished or what is held, run
   `fa brief --report --full`: the same report with every merge, hold, question and
   option. It also records the report time, so the next report counts from then.
   With `presence.report: false` in the config `fa brief --report` prints the plain
   briefing instead and records nothing.

   On the first run in an empty folder (no `.foragent.yml`), `fa brief --report` prints
   the big squirrel and a greeting. Show both exactly as printed (the squirrel in a code
   block), then let the greeting lead into the setup interview.

   The report makes no GitHub call. When an update is available it ends with the update
   line from the last daily check; relay it as written.

2. **Brief.** Run `FORAGENT_UPDATE_CHECK=off fa brief --json` in the project folder for the
   facts behind the choices. The switch keeps `/fa` offline: the report already carries
   the update line, so this second command must not look up a release.
   - If it prints `"setup": false` (no config in this folder), follow the init flow after
     the greeting: run `fa doctor --setup --json` and read its `items` top to bottom in
     the returned order (git installed, gh installed, uv installed, gh logged in, gh
     project scope, repository linked). A nonzero exit with JSON reports unfinished setup.
     Show each failing item's exact `fix` for this OS.
     - For an install item, ask with AskUserQuestion (or the available question tool)
       whether to install it (recommend installing, offer waiting), and run the install
       command only after the person agrees to that item. On Linux the fix links the
       official install page: read it, show the applicable command, and ask before
       running it. If they decline, pause setup.
     - Ask the same way before creating or linking a repository, and run the chosen
       command only after the answer.
     - An item that waits on another item must wait: complete the earlier prerequisite
       first, then refresh the checklist. Never try an auth command while gh is missing.
     - For login, the person runs `! gh auth login` in their own session; show it and
       wait. For permissions, show the exact command or settings pane and wait too.
       FA never runs login or permission changes and never asks for, reads or stores a
       token.
     - After a fix or the person's confirmation, rerun `fa doctor --setup --json` and
       continue from the first failing item.
     With no question tool, stop and print the next command and repository choices.
     Read `skills/fa-init/SKILL.md` (the `fa-init` skill) for the choices and fixes,
     finish the checklist, then run `fa init` in the background after agreement and
     continue to the first order. Reuse completed answers when entering that flow.
     Stop this skill there.
   - Otherwise, when the person asks for more than the report, show the briefing in four
     short parts, each one line or a short list:
     - **Working (하는 일):** `working` — lane, issue, title, phase and minutes.
       Mention `queued` in the same part as a count, with what each waits for if any.
     - **To see (볼 일):** `to_see` — decisions (`needs-human`, with `reason`) and PRs
       waiting for review (`pr-open`). Add `questions_open` if it is nonzero.
     - **Team (팀):** `team` — each lane's title and executor, with fixed and used skills.
       Name lanes with the `no-skills` warning.
     - **Current state (현재 상태):** `profile` (kind, stage, priority), `usage`, and
       `as_of`. If `stale` is true, say the facts may be old.
     Leave out empty parts in one phrase ("nothing running") rather than listing nothing.
   - When the person wants to plan, `fa decisions --json` lists decisions in state
     `decided, nothing ordered` (no linked issue yet). Mention them as candidates only
     if they help the choice; a missing link does not prove the work was never done,
     and nothing is ordered without the person.
   - When `scorecard_line` is not null, show it word for word as its own line after the
     four parts, just before the closing question. It names the metric that got most
     worse against recent days. When it is null, say nothing about the scorecard.

3. **Confirm the profile once per session.** The first time in this session, ask the user
   to confirm the current state in `profile` (stage and priority change over time; kind
   rarely does). Confirm it only once in the session; do not ask again in later briefings
   or orders. If the user changes it, run
   `fa profile set --kind "<kind>" --stage "<stage>" --priority "<priority>"`.
   If `profile` is empty, skip this step; the planning interview fills it.

4. **Open the dashboard yourself when work starts.** The dashboard is the live terminal
   screen of the work. Run `fa gate watch --open` in the project folder once at the start
   of every session; it opens the dashboard beside this planning session. When the
   dashboard is already open, it leaves it as it is; then say it is already open.
   Never tell the person to type a dashboard command: opening it is your job.

5. **Ask each waiting decision, then what to do.** When the report counts decisions
   waiting, ask them right after the report, one at a time, each as its own
   AskUserQuestion (or the available question tool), never pasted as a block of text or
   a list. The decisions are the open orchestrator questions (`fa orch questions --json`:
   each has `id`, `question`, `options` and `recommend`, the 1-based number of the
   recommended option) and the open `needs-human` issues: run `fa inspect prepare`, then
   `fa inspect --json`, and take the `items` whose `group` is `decision` (`number`,
   `summary`, and the gate's options in `details`). Skip a needs-human item whose
   `answered` is true. For each, show the question in one or two sentences and the
   gate's options as the choices, with the recommended option first and marked as
   recommended, plus "Later".
   Do not invent options or recommendations; without options, take the person's own
   words. Record each answer where it came from: `fa orch answer <id> --text "<answer>"`
   for an orchestrator question, `fa inspect answer N --text "<answer>"` for a
   needs-human issue. "Later" records nothing and moves to the next decision.

   Then end the briefing with `next.question` and offer `next.choices`
   with AskUserQuestion (or the available question tool). Show each `label` exactly as
   given; the words come from the messages registry (`src/foragent/messages/brief.py`).
   In Korean they read: "무엇을 할까요?" [기획 인터뷰] [디자인 인터뷰] [볼 것 처리].
   Put the recommended one first: `inspect` (볼 것 처리) when `to_see` or
   `questions_open` is nonzero, else `order` (기획 인터뷰).
   - `order` (기획 인터뷰, planning interview) — decide what to build.
   - `design` (디자인 인터뷰, design interview) — decide how it should look and feel (D73).
   - `inspect` (볼 것 처리) — go through decisions and reviews.

6. **Route.**
   - Inbox: if the user chooses `inspect` (볼 것 처리, the to_see items), follow the inspect flow:
     read `skills/fa-inspect/SKILL.md` (the `fa-inspect` skill) and do it.
   - Design: if the user chooses `design` (디자인 인터뷰), follow the design session flow in
     `skills/fa-design-session/SKILL.md` (the `fa-design-session` skill) in this window.
   - Otherwise (`order`, or any other request to build or change work), follow the
     order flow: read `skills/fa-order/SKILL.md` (the `fa-order` skill) and do it.
     Pass on the profile you just confirmed so the order does not ask again.

7. **Running work.** When work is running (`working` or `queued` is not empty), check the
   orchestrator before you route. This session is the planning session (D76). It never becomes the orchestrator and never
   takes over its work; it only makes sure one runs and talks to it:
   - Run `fa orch alive --json`. If it prints `"alive": false`, run `fa orch ensure`. It
     opens one orchestrator in the background and prints one line; when it opened nothing,
     tell the user that line. If the orchestrator is already alive, do not start a second one.
   - Send every request to the orchestrator twice: as a cross-session message to the
     `orch: <project>` session (SendMessage) and with
     `fa orch request --from planning --text "<request>"`, so it survives a restart.
   - While this session is open, renew `fa plan heartbeat` about every minute, so
     questions reach this session by message instead of a push notification.
   - Show the open questions from `fa orch questions` and record answers with
     `fa orch answer <id> --text "<answer>"`.

8. **Lightest fix before adding a role.** When someone asks for a new standing role (for example "a security owner"), first look
for a cheaper fit and propose it instead. Try these in order and stop at the first that fits:
   1. A line in the lane memory: append one `- [id:<unique id>] <lesson>` line to the end of
      `.claude/agent-memory/fa-<lane>/MEMORY.new.md` of the lane that owns the area. The lane
      dream alone folds pending lessons into `MEMORY.md` and records their ids in
      `MEMORY.consumed.ids`; never edit those two files. The orchestrator commits the
      appended line in its next batched commit.
   2. A skill attached to the lane that owns the area: `fa lanes skills <lane> <skill-id>`
      (the lanes code is the one writer of lane skills).
   3. A reviewer perspective scoped to paths: add a `review_perspectives` entry
      (`{name, focus}`) whose focus names the paths, for example security on
      `src/payment/**`. This needs no new schema.
   4. A new role only when the work must judge across areas and send proposals.
   In the multiple-choice confirmation, show the cheaper option first, with the reason it
   fits; offer the role after it. Only after the person confirms a role, run
   `fa role add <name> --scope "<paths>" --signal "paths:<paths>" --skill <skill-id> --why "<confirmed reason>"`
   with the name, scope, call conditions (repeat `--signal`) and skills (repeat `--skill`).
   Every role may be idle. Roles never message the person; everything goes through the
   orchestrator.

When a flow finishes, tell the user the next step is `/fa`.
