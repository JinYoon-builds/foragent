# Lane-screen wheel scrolling

Source: person, 2026-10-31.

A mouse wheel never changes the selected acorn in `fa gate watch`. Overflowing
content scrolls with the wheel. A fitting view ignores the wheel. Keyboard
Up/Down still chooses acorns.

The existing view lane owns `src/foragent/watch*.py`. This work starts after
running lane-screen issues #1062 and #1064 finish; their files overlap. This
one-issue plan has no internal dependency keys.

The locked acceptance file drives the public `foragent.watch.run` entry point
with the existing offline snapshot fixtures and a fake curses screen. It checks
physical wheel input through a mode-aware terminal, direct KEY_MOUSE input for
both BUTTON4/BUTTON5, tall and fitting content, and Enter's actual issue details.
It does not replace production input, selection, rendering, or viewport logic.

Assumptions: preserve the existing one-row wheel step; one keyboard Down chooses
the next acorn and Up returns to the previous acorn, including in a fitting view.
A selected acorn remains Enter's target even when scrolling moves it offscreen.

Implementing the fix, changing unrelated screen behavior, and modifying the
locked acceptance file are outside this planning run. The lane chooses the
implementation. Run the published acceptance file with:

```sh
uv run pytest -q tests/acceptance/test_wheel_scroll.py
```
