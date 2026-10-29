"""Test package.

Puts the repo root on `sys.path` once, so every test module can `import
runcat_poll` plainly. Appended rather than prepended so nothing here can shadow a
standard library module.
"""

import sys
from pathlib import Path

_REPO = str(Path(__file__).resolve().parent.parent)
if _REPO not in sys.path:
    sys.path.append(_REPO)
