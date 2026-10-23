---
name: fa-design-session
description: Start a design interview in the planning session when the user wants to choose a visual direction, compare references or refine UI drafts. Build a local reference board, compare running drafts in the real medium, and leave an implementation handoff for the orchestrator.
---

# fa-design-session

Design mode continues in the same window as the planning session. Keep the
conversation here while the right pane shows references or running drafts next
to the lane view. The gate schedules implementation; it never watches this
conversation, counts it as a lane worker, or times it out.

Read the request, project decisions, existing screens and `docs/design/CONTRACT.md`
if present. Establish the intended medium and what remains undecided. Offer
choices with a recommendation when a product decision needs the user's answer.
Use the project's language. Carry settled decisions forward instead of repeating
the interview.

## Start with a design interview

Ask 2–3 short questions: who will see this and when, which medium they will use,
and what it should feel like. Offer a few choices with a recommendation; let the
user answer in their own words too. Use answers already given rather than asking
again. People often recognise their taste only after seeing examples, so keep
these questions brief and let the reference comparison reveal the rest.

As soon as the first answers reveal the medium, open its research pack below
and read `~/.local/share/foragent/media/<medium>.md` if it exists. Resolve `media/`
relative to this `SKILL.md` when the skill is installed in another project; the
table shows the source repository paths. The pack gives places to look for
references and pitfalls to check. The external file holds lessons shared across
projects; a missing file does not block the interview.

| Medium | Research pack | External lesson filename |
| --- | --- | --- |
| Web | `skills/fa-design-session/media/web.md` | `web.md` |
| Mobile | `skills/fa-design-session/media/mobile.md` | `mobile.md` |
| Terminal UI (TUI) | `skills/fa-design-session/media/tui.md` | `tui.md` |
| Pixel art | `skills/fa-design-session/media/pixel.md` | `pixel.md` |
| Plain CLI output | `skills/fa-design-session/media/cli.md` | `cli.md` |

For mixed media, read the relevant packs together, for example terminal UI and
pixel art. Sources include Mobbin for web/mobile, lazygit for terminal UI, and
Lospec for pixel palettes; use the pack to choose sources for this project.
While questions continue, have another agent research references in the background.
Give it the medium, audience, constraints, relevant pack and existing lessons;
ask for source links, contrasting directions, reasons each reference fits, and
pitfalls to check in the preview. Bring its findings into this conversation;
the planning session keeps the user's context and presents the choices.

## References before drafts

Collect references in deliberately different directions, including neighbouring
fields such as games, print, objects and pixel art. Spread about nine references
over three directions, roughly three each. Stretch axes such as cute–serious,
dense–airy and colourful–mono so the alternatives reveal real differences;
the numbers are defaults, while the purpose is to make taste visible. Choose
appropriate sources for the medium rather than treating every project as a website.

Never commit third-party reference images. Keep source links and notes of what
was referenced in `docs/design/<topic>/`; images stay in external local state.
Do not copy reference art: draw new art, using the references to understand
qualities such as shape, spacing, colour and hierarchy.

Build a local browser reference board. Supply a JSON file outside the checkout:

```json
{"references": [
  {"id":"airy-web", "direction":"Airy", "title":"Spacious dashboard",
   "kind":"web", "url":"https://example.com"},
  {"id":"dense-tui", "direction":"Dense", "title":"Terminal example",
   "kind":"image", "url":"https://example.org/original", "image":"/absolute/local/example.png"}
]}
```

`kind: web` captures a headless Playwright screenshot; install Playwright and its
Chromium browser if needed. For existing screenshots or GIFs use `kind: image`;
relative image paths resolve against the manifest directory. Use gstack or
Playwright MCP to research and capture additional references when appropriate.
The manifest and copied images stay local, outside git. The board groups local
thumbnails by direction and opens each original on click.

```sh
fa design preview --stage board --topic dashboard --references /tmp/references.json
fa design picks --topic dashboard --json
```

Have the user click 👍/👎. Likes and dislikes are saved to a file (`picks.json`)
in the project's external state directory and can be read by the planning session
with `fa design picks`. Read those saved picks, ask what matters in ambiguous
choices, and extract shared principles in words. Treat dislikes as equally useful
evidence; the user can pick likes and dislikes without explaining each click.
Record the principles in `docs/design/<topic>/HANDOFF.md`, with reference links
and notes explaining what to carry forward or avoid. Do not infer an entire visual
style from one click or make the user retype their selections.

## Compare running drafts

Make 2–3 runnable code drafts in `docs/design/<topic>/`; each draft names the
reference it builds on and the extracted principles it explores. Never show only
one draft and ask “how is it?” during the comparison. Drafts are running code in
the real medium: use them to discover
movement, clicks, colour, layout and width problems before implementation.
Choose a suitable tool; find a tool and propose it if the needed capability is
missing.

- Terminal UI: use existing Orca panes or tmux splits to execute scripts side by
  side. Repeat `--command` for each draft. Shell-quote paths with spaces inside
  each command. `fa design done` closes only the saved preview panes, preserving
  the planning pane and lane view.
- Web UI: start a local server for each runnable draft. Repeat `--url` to embed
  the live drafts side by side in one browser comparison page. Test interactions
  in the browser and use gstack screenshots or Playwright MCP to inspect states.
  Ensure the draft servers allow local iframe embedding. A single URL may point
  to a comparison page already made by the planning agent.
- Mobile: use a mobile-sized running web draft or simulator and screenshots.

```sh
fa design preview --stage drafts --topic dashboard \
  --command 'python3 docs/design/dashboard/a.py' \
  --command 'python3 docs/design/dashboard/b.py'
# Or, with both local servers already running:
fa design preview --stage drafts --topic dashboard \
  --url http://127.0.0.1:8766 --url http://127.0.0.1:8767
```

Figma MCP is an optional tool for existing design files or a team already working
with designers in Figma. The normal preview uses running code; choose Figma when
it helps that team's workflow and preserve links in the handoff.

After comparison, refine the chosen draft in the preview. The command accepts a
single running draft for this refinement stage. A new preview replaces the
previous preview panes. Keep the executable source and decisions durable; keep
third-party reference images in local state rather than committing them.

## Leave an implementation handoff

Write `docs/design/<topic>/HANDOFF.md` with:

- The chosen draft, its source files and exact run command, viewport/terminal
  width, relevant interaction states and how to capture the result.
- Decisions, visual principles, reference source links and the reasons behind
  the selections. Refer to saved picks without copying local reference images.
- What implementation must preserve, what is still unresolved, and visual
  Done when evidence: the same screen as the draft, plus flow/accessibility
  checks appropriate to this medium.

Run `fa design done` to return the right pane to the lane view and stop the local
board server. It leaves draft sources, reference assets and picks available for
later work. Draft application servers remain owned by the planning session;
stop those explicitly when finished with them.

Pass the handoff to the orchestrator with
`fa orch request --from planning --text "Implement docs/design/<topic>/HANDOFF.md"`.
This records a durable request; it does not publish or approve issues. The
orchestrator reads the handoff, splits it into lane issues with visual Done when
checks, and uses the normal order approval workflow. The user normally does not
talk to the implementing worker: the draft and handoff provide the needed
context. Keep later changes in the handoff and the order rather than requiring
repeated explanations to workers.
