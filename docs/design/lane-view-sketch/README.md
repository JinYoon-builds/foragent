# Lane view sketch (D64)

Throwaway sketch of the squirrel lane view, drawn while deciding D64. It is a
reference for the real implementation in `src/foragent/watch.py`, not product code.

Run it in a terminal pane (truecolor, ideally 56+ columns):

    python3 docs/design/lane-view-sketch/tui_narrow.py

Enter moves to the next scene (prep → publish → running → done). `l` or a click on
the orchestrator's speech bubble opens the log. Resize the pane to see the layout
switch between side-by-side (56+ columns) and stacked (44–55 columns).

- `cast.py`: pixel grids — big orchestrator squirrel, GitHub tree, small lane squirrels.
- `anim.py`: half-block renderer, numbered acorns, acorns flying to the tree.
- `tui.py`: wide first draft (about 100 columns) — too wide for the side pane.
- `tui_narrow.py`: the side-pane version to follow.

The squirrel is original pixel art in the style of a reference image Jin shared;
that reference image is not part of this repository.
