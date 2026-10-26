---
name: fa
description: The single Foragent entry point. Brief the user on the project, confirm the profile once per session, and route to setup, the inbox or a new order. Use whenever the user types /fa or asks what Foragent is doing.
---

People type only `/fa`. The command names `/fa-init`, `/fa-inspect` and `/fa-order` stay,
but you open them yourself; never ask the user to type them (D59 addendum 2).

Speak in the shared Foragent voice from `docs/voice.md`: short sentences, plain words,
active voice; in Korean use 해요체. Speak the language in the brief (`language` from the
config, else the user's language). Preserve exact commands, URLs and issue numbers.

1. **Brief.** Run `fa brief --json` in the project folder.
   - If it prints `"setup": false` (no config in this folder), say so in one line and
     follow the init flow: read `skills/fa-init/SKILL.md` (the `fa-init` skill) and do it.
     Stop this skill there.
   - Otherwise show the briefing in four short parts, each one line or a short list:
     - **Working (하는 일):** `working` — lane, issue, title, phase and minutes.
       Mention `queued` in the same part as a count, with what each waits for if any.
     - **To see (볼 일):** `to_see` — decisions (`needs-human`, with `reason`) and PRs
       waiting for review (`pr-open`). Add `questions_open` if it is nonzero.
     - **Team (팀):** `team` — each lane's title and executor, with fixed and used skills.
       Name lanes with the `no-skills` warning.
     - **Current state (현재 상태):** `profile` (kind, stage, priority), `usage`, and
       `as_of`. If `stale` is true, say the facts may be old.
     Leave out empty parts in one phrase ("nothing running") rather than listing nothing.

2. **Confirm the profile once per session.** The first time in this session, ask the user
   to confirm the current state in `profile` (stage and priority change over time; kind
   rarely does). Confirm it only once in the session; do not ask again in later briefings
   or orders. If the user changes it, run
   `fa profile set --kind "<kind>" --stage "<stage>" --priority "<priority>"`.
   If `profile` is empty, skip this step; the planning interview fills it.

3. **Ask what to do.** End the briefing with `next.question` and offer `next.choices`
   with AskUserQuestion (or the available question tool). Show each `label` exactly as
   given; the words come from the messages registry (`src/foragent/messages/brief.py`).
   In Korean they read: "무엇을 할까요?" [기획 인터뷰] [디자인 인터뷰] [볼 것 처리].
   Put the recommended one first: `inspect` (볼 것 처리) when `to_see` or
   `questions_open` is nonzero, else `order` (기획 인터뷰).
   - `order` (기획 인터뷰, planning interview) — decide what to build.
   - `design` (디자인 인터뷰, design interview) — decide how it should look and feel (D73).
   - `inspect` (볼 것 처리) — go through decisions and reviews.

4. **Route.**
   - Inbox: if the user chooses `inspect` (볼 것 처리, the to_see items), follow the inspect flow:
     read `skills/fa-inspect/SKILL.md` (the `fa-inspect` skill) and do it.
   - Design: if the user chooses `design` (디자인 인터뷰), follow the design session flow in
     `skills/fa-design-session/SKILL.md` (the `fa-design-session` skill) in this window.
   - Otherwise (`order`, or any other request to build or change work), follow the
     order flow: read `skills/fa-order/SKILL.md` (the `fa-order` skill) and do it.
     Pass on the profile you just confirmed so the order does not ask again.

5. **Running work.** When work is running (`working` or `queued` is not empty), check the
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

When a flow finishes, tell the user the next step is `/fa`.
