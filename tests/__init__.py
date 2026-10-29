"""Test package.

Puts the repo root on `sys.path` once, so every test module can `import
runcat_poll` plainly. Appended rather than prepended so nothing here can shadow a
standard library module.
"""

import os
import sys
import time
from pathlib import Path

# A Card states each Reset on the local clock, so every expected Card string
# depends on the zone. Pin one without daylight saving so the suite reads the
# same on any machine.
os.environ["TZ"] = "Asia/Seoul"
time.tzset()

_REPO = str(Path(__file__).resolve().parent.parent)
if _REPO not in sys.path:
    sys.path.append(_REPO)
