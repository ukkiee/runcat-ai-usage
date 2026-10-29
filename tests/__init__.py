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

# The cards leave out the models named in the user's own settings file. Point
# every test at an empty one, so hiding Fable on this machine cannot change what
# the suite expects to see.
import tempfile  # noqa: E402

import runcat_poll  # noqa: E402

runcat_poll.SETTINGS_FILE = Path(tempfile.mkdtemp(prefix="runcat-tests-")) / "settings.json"
