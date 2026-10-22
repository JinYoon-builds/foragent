---
name: fa-design-session
description: Explore UI and UX with the user in an interactive design session. Show two or three working drafts together, revise them from visual feedback, and hand the chosen design to the orchestrator for implementation. Use for screen design, design foundations, layout or style choices, and fa design open.
---

# fa-design-session

Work beside the orchestrator in the `design: <project>` tab. This is a conversation
with the user; a quiet session is not stalled work. The gate does not watch this
session, allocate a lane to it, restart it, or close it.

Read the project's language, design foundation (`docs/design/CONTRACT.md`), settled
decisions and existing UI before drawing. Reuse the project's chosen style skills
and voice; the `fa-design` agent configuration supplies fixed skills when present.
If its usual lane instructions describe autonomous implementation, use this
interactive workflow for this session. Treat the supplied topic as a starting
point, or settle the topic from the conversation when none was supplied.

## Show working alternatives

Create **2–3 drafts**, labelled A/B/C, and show them **side by side** with the same
content, state and viewport so the differences are easy to judge. Never show only
one draft and ask “how is it?”. Make the alternatives meaningfully different in
layout or interaction, rather than only changing a color. Recommend one and
explain its tradeoff in a sentence; use 2–3 choices for unresolved taste decisions.
Start with something the user can see, then revise from their reaction until they
choose a draft. Keep alternatives available while comparing revisions.

Choose the preview tool for the actual target and the tools the session can use:

| Target | Useful preview |
| --- | --- |
| Terminal/TUI | Use the reserved Orca preview pane and additional Orca terminal splits to display drafts together at realistic terminal sizes. In tmux, use its preview pane and splits. |
| Web | Start a local server and open the draft in a browser: Orca's embedded browser, gstack screenshots, or Playwright MCP as available. Put alternatives on a comparison page so they can be viewed together. |
| Existing design files | Use Figma MCP when available to inspect the design files and compare frames against the proposed revisions. |
| Mobile | Run the simulator and capture screenshots for the same device size and state; display alternatives together. |

Discover available tools before choosing. If the right preview tool is missing,
find a tool and propose it with a recommendation; use an available preview while
the choice is pending. Per-lane MCP installation is a separate task. The opener
reserves a terminal preview pane; adapt it or open the appropriate browser surface
after inspecting the target. Read the session record supplied by `fa design open`
to find the exact preview handle; check it before sending commands into that pane.

## Keep the design durable

Keep draft code and assets in `docs/design/<topic>/`, with a short stable topic
directory. Store screenshots, preview commands/URLs and a decision log there.
Record the alternatives, what changed, the user's decision and why. Use relative
links so a later session or implementation lane can reproduce what was compared.
Prototype in this folder so implementation lanes can keep working independently.
Design foundations can be explored here before becoming implementation issues.

When the user chooses a design, write `docs/design/<topic>/HANDOFF.md` containing:

- The goal, scope and **chosen draft**, with links to runnable code, assets and
  screenshots; identify the selected revision and the user's decision.
- How to run the preview, exact screen/route, viewport or terminal/device size,
  fixture data and interaction states (including loading, empty and error states
  where relevant).
- Settled layout, tokens, components, accessibility and copy decisions, plus any
  remaining questions clearly separated from decisions.
- Implementation areas and dependencies the orchestrator can split by lane.
- **Done when**: the implementation shows the **same screen as the draft** under
  those conditions. Include reproducible functional checks and the reference
  screenshot; request result screenshots for visual comparison after completion.

Hand the HANDOFF.md path to the **orchestrator** through the existing project
handoff channel, or leave the path as the next input for that session when no
channel is available. The orchestrator turns it into lane issues using `fa-order`
and the existing publication approval flow. Choosing a draft settles design taste;
publication and implementation scope follow that flow. Keep the design session
independent of gate monitoring throughout.
