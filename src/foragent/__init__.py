"""Foragent: your agent forages for the next task."""

import os as _os
import sys as _sys

__version__ = "0.1.0"

# An installed copy lives in the tool directory that `uv tool install --reinstall`
# empties. Writing bytecode there races the reinstall ("Directory not empty"),
# whatever environment launchd or a caller started this process with, so an
# installed Foragent never writes it. A source checkout keeps the default.
if "site-packages" in _os.path.normpath(__file__).split(_os.sep):
    _sys.dont_write_bytecode = True
