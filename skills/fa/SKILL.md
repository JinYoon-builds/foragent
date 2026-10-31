---
name: fa
description: The single Foragent entry point. Brief the user on the project, confirm the profile once per session, and route to setup, the inbox or a new order. Use whenever the user types /fa or asks what Foragent is doing.
---

People type only `/fa`. The command names `/fa-init`, `/fa-inspect` and `/fa-order` stay,
but you open them yourself; never ask the user to type them (D59 addendum 2).

Speak in the shared Foragent voice from `docs/voice.md`: short sentences, plain words,
active voice; in Korean use 해요체. Speak the language in the brief (`language` from the
config, else the user's language). Preserve exact commands, URLs and issue numbers.

1. **Report.** On each `/fa`, run `fa brief --report` in the project folder first and
   show its output to the person word for word, in a code block. It leads with the
   current judgment, then what finished since the previous report, what is happening
   now and what the person should do. Running it records the report time, so run it once
   per `/fa`. With `presence.report: false` in the config it prints the plain briefing
   instead and records nothing.

   On the first run in an empty folder (no `.foragent.yml`), `fa brief --report` prints
   the big squirrel and a greeting. Show both exactly as printed (the squirrel in a code
   block), then let the greeting lead into the setup interview.

   The report makes no GitHub call. When an update is available it ends with the update
   line from the last daily check; relay it as written.

2. **Brief.** Run `FORAGENT_UPDATE_CHECK=off fa brief --json` in the project folder for the
   facts behind the choices. The switch keeps `/fa` offline: the report already carries
   the update line, so this second command must not look up a release.
   - If it prints `"setup": false` (no config in this folder), follow the init flow after
     the greeting: read `skills/fa-init/SKILL.md` (the `fa-init` skill) and do it.
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
   - When `scorecard_line` is not null, show it word for word as its own line after the
     four parts, just before the closing question. It names the metric that got most
     worse against recent days. When it is null, say nothing about the scorecard.

3. **Confirm the profile once per session.** The first time in this session, ask the user
   to confirm the current state in `profile` (stage and priority change over time; kind
   rarely does). Confirm it only once in the session; do not ask again in later briefings
   or orders. If the user changes it, run
   `fa profile set --kind "<kind>" --stage "<stage>" --priority "<priority>"`.
   If `profile` is empty, skip this step; the planning interview fills it.

4. **Ask what to do.** End the briefing with `next.question` and offer `next.choices`
   with AskUserQuestion (or the available question tool). Show each `label` exactly as
   given; the words come from the messages registry (`src/foragent/messages/brief.py`).
   In Korean they read: "무엇을 할까요?" [기획 인터뷰] [디자인 인터뷰] [볼 것 처리].
   Put the recommended one first: `inspect` (볼 것 처리) when `to_see` or
   `questions_open` is nonzero, else `order` (기획 인터뷰).
   - `order` (기획 인터뷰, planning interview) — decide what to build.
   - `design` (디자인 인터뷰, design interview) — decide how it should look and feel (D73).
   - `inspect` (볼 것 처리) — go through decisions and reviews.

5. **Route.**
   - Inbox: if the user chooses `inspect` (볼 것 처리, the to_see items), follow the inspect flow:
     read `skills/fa-inspect/SKILL.md` (the `fa-inspect` skill) and do it.
   - Design: if the user chooses `design` (디자인 인터뷰), follow the design session flow in
     `skills/fa-design-session/SKILL.md` (the `fa-design-session` skill) in this window.
   - Otherwise (`order`, or any other request to build or change work), follow the
     order flow: read `skills/fa-order/SKILL.md` (the `fa-order` skill) and do it.
     Pass on the profile you just confirmed so the order does not ask again.

6. **Running work.** When work is running (`working` or `queued` is not empty), check the
   orchestrator before you route. This session is the planning session (D76). It never becomes the orchestrator and never
   takes over its work; it only makes sure one runs and talks to it:
   - Run `fa orch alive --json`. If it prints `"alive": false`, run `fa orch ensure`. It
     opens one orchestrator in the background and prints one line; when it opened nothing,
     tell the user that line. If the orchestrator is already alive, do not start a second one.
   - Open the lane view beside this planning session with `fa gate watch --open`.
   - Send every request to the orchestrator twice: as a cross-session message to the
     `orch: <project>` session (SendMessage) and with
     `fa orch request --from planning --text "<request>"`, so it survives a restart.
   - While this session is open, renew `fa plan heartbeat` about every minute, so
     questions reach this session by message instead of a push notification.
   - Show the open questions from `fa orch questions` and record answers with
     `fa orch answer <id> --text "<answer>"`.

7. **Lightest fix before adding a role.** When someone asks for a new standing role (for example "a security owner"), first look
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
