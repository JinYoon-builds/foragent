# Terminal UI research pack

## References

- lazygit, btop and k9s: dense, keyboard-first terminal screens.
- Bubble Tea and Ratatui example galleries: widgets and layouts.
- Neighbouring fields: old BBS screens, flight instruments, train departure boards.

Save the link and a short note per reference. Do not commit screenshots or GIFs.

## Pitfalls

Lessons from 2026-10-22 (the lane view):

- Ambiguous-width glyphs: in Korean (CJK) fonts a symbol such as `◀` draws two
  cells wide and pushes the border. Do not use ambiguous-width symbols in
  borders or aligned columns.
- Minimum widths: plan for 56 columns or more (recommended) and 44–55 columns
  (minimum, stack instead of side by side); below 44 columns fall back to one
  text line per item. Measure the width on every redraw.
- Alternate screen: enter it for full-screen views and always restore the
  terminal on exit or crash.
- Wheel vs click: mouse wheel and click events arrive differently; do not treat
  a scroll as a click.
- No-colour terminals: respect `NO_COLOR` and keep meaning readable without
  colour.
