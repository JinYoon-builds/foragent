"""Sections of the running lane view, one module per visible group.

Every module here is a section: ``ORDER`` is a unique integer and
``render(snapshot, config, repo_root, **context)`` returns the section's lines
(``watch_cells.Line``). ``watch_layout._frame`` discovers the modules over this
package's ``__path__`` on every frame and stacks them by ``ORDER`` with one blank
line between non-empty sections; a section that returns ``[]`` adds nothing.
A module may set ``GAP = False`` to follow the previous section without the
blank line. Context carries ``tick``, ``width``, ``selected``, ``now`` and
``layout`` from the frame API, plus ``top`` (the section's first row) and
``hits`` (clickable rectangles in frame rows, later entries win).

Shared helpers live in ``foragent.watch_layout``; this package holds sections only.
"""
