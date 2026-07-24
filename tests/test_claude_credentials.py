"""Where the Claude credential is read from — and only ever read.

The Keychain is Claude Code's credential home on a desktop login, but a headless
or remote login writes `~/.claude/.credentials.json` instead, and there the
Keychain holds no item at all. These tests pin which store answers when, so a
machine of either kind polls live usage rather than silently decaying to a Stale
Reading.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import runcat_poll as mod  # the tests package puts the repo root on sys.path


KEYCHAIN_OAUTH = {"accessToken": "from-keychain", "expiresAt": 1784898465430}
FILE_OAUTH = {"accessToken": "from-file", "expiresAt": 1784898465430}


class Result:
    """What `subprocess.run` hands back, reduced to the two fields we read."""

    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class FakeSubprocess:
    """Stands in for the `subprocess` module, keeping the exception identity the
    caller catches."""

    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, run):
        self.run = run


class ClaudeCredentialCase(unittest.TestCase):
    """A temporary ~/.claude holding the file store, with module state restored."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.creds_path = Path(self.tmp.name) / ".credentials.json"
        self.logs = []
        self.write_file_store()
        self.patch("CLAUDE_CREDS_FILE", self.creds_path)
        self.patch("log", self.logs.append)

    def patch(self, name, value):
        original = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(lambda: setattr(mod, name, original))

    @property
    def log_text(self):
        return " ".join(self.logs)

    def write_file_store(self, blob=None):
        blob = {"claudeAiOauth": FILE_OAUTH} if blob is None else blob
        self.creds_path.write_text(json.dumps(blob), encoding="utf-8")

    def patch_keychain(self, returncode, stdout=""):
        def run(*_args, **_kwargs):
            return Result(returncode, stdout)

        self.patch("subprocess", FakeSubprocess(run))

    def patch_keychain_prompt(self):
        def run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="security", timeout=15)

        self.patch("subprocess", FakeSubprocess(run))


class ClaudeReadTokenTest(ClaudeCredentialCase):
    def test_the_keychain_is_the_first_place_we_look(self):
        """Both stores can hold a credential at once; the Keychain is the one
        Claude Code keeps current on a desktop login."""
        self.patch_keychain(0, json.dumps({"claudeAiOauth": KEYCHAIN_OAUTH}))

        self.assertEqual(mod.claude_read_token(), KEYCHAIN_OAUTH)

    def test_the_file_store_answers_when_the_keychain_has_no_item(self):
        self.patch_keychain(44)  # `security`'s "item could not be found"

        self.assertEqual(mod.claude_read_token(), FILE_OAUTH)

    def test_a_keychain_blob_without_the_oauth_field_falls_through(self):
        self.patch_keychain(0, json.dumps({"somethingElse": {}}))

        self.assertEqual(mod.claude_read_token(), FILE_OAUTH)

    def test_keychain_output_we_cannot_read_falls_through(self):
        self.patch_keychain(0, "not json at all")

        self.assertEqual(mod.claude_read_token(), FILE_OAUTH)

    def test_neither_store_holding_it_reads_as_absent(self):
        self.patch_keychain(44)
        self.creds_path.unlink()

        self.assertIsNone(mod.claude_read_token())

    def test_a_file_store_we_cannot_read_reads_as_absent(self):
        """Unreadable is not the same as empty, but neither yields a token, and
        guessing at one would poll with a credential we do not have."""
        self.patch_keychain(44)
        self.creds_path.write_text("{ this is not json", encoding="utf-8")

        self.assertIsNone(mod.claude_read_token())

    def test_a_file_store_without_the_oauth_field_reads_as_absent(self):
        self.patch_keychain(44)
        self.write_file_store({"somethingElse": {}})

        self.assertIsNone(mod.claude_read_token())

    def test_a_keychain_prompt_is_not_answered_by_the_file_store(self):
        """A prompt means the Keychain does hold the item — the read is blocked,
        not missing — and under launchd nobody can answer it. Falling through
        here would poll with whatever stale copy the file store still holds."""
        self.patch_keychain_prompt()

        self.assertIsNone(mod.claude_read_token())
        self.assertIn("keychain read timed out", self.log_text)


class ClaudeCredentialWriteTest(ClaudeCredentialCase):
    def test_reading_the_file_store_never_writes_it_back(self):
        """The read-only rule covers both stores: ADR 0001 rejects writing the
        Keychain, and a file store Claude Code rotates is no safer to write."""
        before = self.creds_path.read_bytes()
        self.patch_keychain(44)

        mod.claude_read_token()

        self.assertEqual(self.creds_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
