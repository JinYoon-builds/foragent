---
name: fa-design-session
description: Guide the planning session's design mode when choosing a screen's visual direction, comparing references, or refining runnable designs. Show a local reference board, compare live drafts in their actual medium, then hand the selected design to the orchestrator.
---

# fa-design-session

Design is a mode of the planning session: continue the conversation in the same
window, using the project's language, voice and chosen style skill. The right pane
normally shows the lane view; preview panes sit beside it during design. The gate
does not watch this conversation, treat silence as a stall, or reserve a worker.

## Design interview and reference board

Start with 2–3 short questions about who will see the screen, when, and the feeling
it should have. Read existing decisions and design files before asking again.
Find references for the actual medium, spreading about nine references across
three directions. Mix neighbouring fields and stretch the visual axes, so the
user can discover a preference by looking rather than inventing a description.
The counts are defaults; what matters is offering meaningfully different choices.

Choose the research and capture tools yourself. For web references use headless
Playwright screenshots (the board CLI uses Playwright CLI, or a locally installed
Playwright through npx); gstack screenshots and Playwright MCP are also useful
when preparing local images. For a terminal screen use screenshots or GIFs of
running TUIs. For mobile use simulator screenshots. If a needed tool is missing,
find a tool and propose it with a recommendation instead of guessing a screenshot.

Prepare a local JSON manifest outside the repository, for example:

```json
{"references": [
  {"id": "airy-web", "direction": "Airy", "title": "Room to read", "kind": "web", "url": "https://example.com"},
  {"id": "dense-tui", "direction": "Dense", "title": "Terminal rhythm", "kind": "image", "url": "https://example.org", "image": "/tmp/tui.png"}
]}
```

```sh
fa design preview --stage board --topic dashboard --references /tmp/references.json
fa design picks --topic dashboard --json
```

The local browser reference board groups thumbnails by direction. Clicking a
thumbnail opens the original; 👍 likes and 👎 dislikes are saved to a file (a JSON
file in the project's local Foragent state, outside git). Read `fa design picks`
after the user chooses; a changed vote replaces the previous vote. Extract the
shared principles from both liked and disliked references and confirm them in
the conversation. Keep links and notes about what was useful in `docs/design/<topic>/`.
Never commit reference images: they stay local. Do not copy other people's artwork;
use references to understand style and make original work.

## Running drafts in the real medium

Build 2–3 distinct drafts as running code, each linked to the reference and
principles it builds on. Never show only one draft and ask "how is it?" at the
comparison stage. Store runnable code and decisions in `docs/design/<topic>/` so
the implementing worker can reproduce what the user saw.

Choose the preview tool for the actual medium:

- Terminal: run drafts side by side in Orca panes (tmux splits also work). Pass
  each full shell command explicitly; quote paths containing spaces.
- Web: start each draft's local server, then show one browser comparison page
  with live interactive frames. Keep frame embedding enabled on these local
  servers. Use the browser to verify clicks, widths and motion, not just a picture.
- Mobile: use a mobile sized web comparison page or a simulator. Screenshots help
  review the result, while runnable code remains the source for implementation.

```sh
fa design preview --stage drafts --topic dashboard \
  --command 'python3 docs/design/dashboard/a.py' \
  --command 'python3 docs/design/dashboard/b.py'
fa design preview --stage drafts --topic website \
  --url http://127.0.0.1:3000 --url http://127.0.0.1:3001
```

Figma MCP is optional: use it when the team already uses Figma, works with a
designer, or needs to exchange existing design files. The normal preview uses
running code in the real medium; Figma is an option rather than a required step.

After the user selects a direction, refine the chosen draft in the adjacent pane.
For one selected draft, use `fa design preview --topic TOPIC --command 'COMMAND'`
or `--url URL` without `--stage drafts`. Preview replaces the previous preview;
it keeps the planning conversation and lane view alive. Repeat until the user
chooses the result. Save screenshots of your own final design, decisions and the
commands needed to reproduce it. The agent owns starting/stopping draft servers;
the preview command only opens the supplied commands/URLs.

## Handoff and return to the lane view

Write `docs/design/<topic>/HANDOFF.md` with the selected draft and run command,
reference links and notes, confirmed principles, screenshots of our own design,
interaction and responsive behavior, scope, and unresolved decisions if any.
Include visual Done when: "same screen as the draft" (시안과 같은 화면), checked
with a screenshot at the documented size plus tests of the demonstrated behavior.

Send a durable request to the orchestrator, naming the file and the user's
confirmed choice:

```sh
fa orch request --from planning --text 'Design agreed: docs/design/dashboard/HANDOFF.md. Split into lane issues with Done when: same screen as the draft.'
fa design done
```

The orchestrator reads HANDOFF.md and splits it into lane issues using the normal
order approval flow; preview and picks alone create no issue or approval. The
user normally does not talk to the implementation worker: the design agreement
and evidence carry the intent. Review the implemented result against the chosen
draft. `fa design done` closes only saved preview panes and the local comparison
server, returning the right pane to the lane view. Picks and durable designs
remain available for later sessions; repeating done is harmless.
