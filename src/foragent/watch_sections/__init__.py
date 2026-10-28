"""Sections of the running lane view, one module per visible group.

Every module here is a section: ``ORDER`` is a unique integer and
``render(snapshot, config, repo_root, **context)`` returns the section's lines
(``watch_cells.Line``). ``watch_layout._frame`` discovers the modules over this
package's ``__path__`` on every frame and stacks them by ``ORDER`` with one blank
line between non-empty sections; a section that returns ``[]`` adds nothing.
A module may set ``GAP = False`` to follow the previous section without the
blank line, and ``GAP_AFTER = False`` to have the next section follow it
directly. Context carries ``tick``, ``width``, ``selected``, ``now`` and
``layout`` from the frame API, ``height`` (None when unbounded) and ``state``
(the live view's memory across frames, None for a stateless frame), plus ``top`` and ``hits`` (clickable rectangles,
later entries win). A section draws from ``top`` 0; the compositor moves its
hits to the rows where it lands. Within a screen ``height``, sections with
``KEEP = True`` always stay and the others give way from the bottom up; a section
with ``STAYS = True`` stays like KEEP but does not end the lane-row block, and
moves up into the first ``height - 1`` rows when the KEEP sections push it below; the
footer then gets ``short=True``. Before that, a section with ``TIGHT = True`` (the lane
rows) is drawn again with ``tight=True`` and may leave out rows it can spare. ``rows=True`` says the frame has the lane rows,
so a section need not list the running issues again.

Shared helpers live in ``foragent.watch_layout``; this package holds sections only.
"""
