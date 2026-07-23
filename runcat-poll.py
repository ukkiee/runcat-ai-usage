#!/usr/bin/env python3
"""Executable entry point for the RunCat usage poller.

This file's name and location are load-bearing, and permanent. Installed launchd
agents hold this exact absolute path, so moving or removing it would break every
existing install on the next tick — silently, leaving a Card that looks alive
while it quietly stops being true. There is deliberately no removal date and no
migration: the path simply never changes.

The implementation lives beside it in `runcat_poll.py`. The hyphen in this file's
own name makes it unusable as a module name, which is the whole reason the two
are separate: tests and other callers import the implementation directly.

The parent directory is added to `sys.path` explicitly rather than relying on
Python's script-launch behaviour, so this keeps resolving however it is invoked —
as a script, through a symlink, or loaded by tooling. It is appended rather than
prepended so a file here can never shadow a standard library module, and only
when absent so repeated loads don't accumulate duplicates.
"""

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.append(_HERE)

from runcat_poll import main  # noqa: E402 — the lines above are what make this resolvable

if __name__ == "__main__":
    main()
