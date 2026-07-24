"""The executable entry point must keep working exactly where it is.

Installed launchd agents hold the entry point's absolute path, so moving or
removing it would break every existing install on the next tick — silently,
leaving a Card that looks alive while it quietly stops being true. These tests
pin that promise: the path resolves and actually runs a full poll however it is
invoked, and the implementation beside it is importable by name so nothing needs
file-path loading machinery.
"""

import runpy
import subprocess
import sys
import unittest
from pathlib import Path

import runcat_poll  # the tests package puts the repo root on sys.path

REPO = Path(__file__).resolve().parent.parent
ENTRY_POINT = REPO / "runcat-poll.py"
IMPLEMENTATION = REPO / "runcat_poll.py"


class EntryPointTest(unittest.TestCase):
    def test_entry_point_still_lives_at_its_installed_path(self):
        self.assertTrue(
            ENTRY_POINT.is_file(),
            "installed launchd agents point here by absolute path; it must never move",
        )

    def test_entry_point_binds_the_real_main(self):
        namespace = runpy.run_path(str(ENTRY_POINT), run_name="wiring_check")

        self.assertIs(
            namespace.get("main"), runcat_poll.main,
            "the entry point does not reach the implementation's main()",
        )

    def test_entry_point_runs_a_full_poll(self):
        """Run under `__main__` so the guard actually fires, with both Providers
        stubbed. Without this, deleting the guard would leave every test green.

        `main()` now reads the clock once and hands each poll that moment, so the
        stubs take `now` — and record it, turning what would be a TypeError into
        positive proof the clock reaches the seam as one shared number."""
        polled = []
        for name in ("claude_poll", "codex_poll"):
            original = getattr(runcat_poll, name)
            setattr(runcat_poll, name, lambda now, n=name: polled.append((n, now)))
            self.addCleanup(setattr, runcat_poll, name, original)

        runpy.run_path(str(ENTRY_POINT), run_name="__main__")

        self.assertEqual(
            [n for n, _ in polled], ["claude_poll", "codex_poll"],
            "the entry point did not poll both Providers",
        )
        self.assertTrue(
            all(isinstance(now, float) for _, now in polled),
            "main() must hand each poll a numeric moment, never let it read a global",
        )
        self.assertEqual(
            len({now for _, now in polled}), 1,
            "both Providers must render against the one moment the tick read",
        )

    def test_entry_point_resolves_from_an_unrelated_working_directory(self):
        """launchd runs it by absolute path from a directory that is not the repo."""
        result = subprocess.run(
            [sys.executable, "-c",
             f"import runpy; ns = runpy.run_path({str(ENTRY_POINT)!r}, run_name='wiring_check');"
             " assert callable(ns['main'])"],
            cwd="/", capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ImportableImplementationTest(unittest.TestCase):
    def test_implementation_imports_and_works_without_path_machinery(self):
        self.assertTrue(callable(runcat_poll.main))
        # Exercise it, rather than only proving the name resolves.
        self.assertEqual(runcat_poll.codex_window_identity(604800),
                         (runcat_poll.WEEKLY_WINDOW, ""))
        self.assertEqual(runcat_poll.codex_window_identity(18000),
                         (runcat_poll.SESSION_WINDOW, ""))
        self.assertEqual(runcat_poll.label_set("en")["weekly"], "Weekly")

    def test_implementation_name_has_no_hyphen(self):
        """A hyphen is what makes the entry point unimportable; the implementation
        must never acquire one, or the split stops buying anything."""
        self.assertNotIn("-", IMPLEMENTATION.stem)

    def test_implementation_is_not_a_second_executable_path(self):
        """One installed path only. A runnable implementation would let installs
        point at either file and drift apart."""
        self.assertFalse(
            IMPLEMENTATION.read_text(encoding="utf-8").startswith("#!"),
            "the implementation should not carry a shebang",
        )
        self.assertFalse(
            IMPLEMENTATION.stat().st_mode & 0o111,
            "the implementation should not be executable",
        )


if __name__ == "__main__":
    unittest.main()
