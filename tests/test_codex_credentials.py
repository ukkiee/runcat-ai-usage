"""Codex credential rotation must never be lost.

By the time we write back, the server has already rotated the refresh token, so
the copy on disk is dead. Failing quietly there logs the user out of Codex with
no clue why — these tests pin the behaviour that prevents it.
"""

import json
import tempfile
import unittest
from pathlib import Path

import runcat_poll as mod  # the tests package puts the repo root on sys.path


AUTH = {
    "last_refresh": "2026-07-17T07:41:33.823Z",
    "tokens": {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "account_id": "acct-1",
        "id_token": "keep-me",
    },
}


def failing_write(*_args, **_kwargs):
    raise OSError("disk full")


class CodexCredentialCase(unittest.TestCase):
    """A temporary ~/.codex holding a known credential, with module state restored."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)

        self.auth_path = home / "auth.json"
        self.auth_path.write_text(json.dumps(AUTH), encoding="utf-8")
        self.lost_path = home / "runcat-rotation-lost.json"

        self.patch("CODEX_AUTH_FILE", self.auth_path)
        self.patch("CODEX_ROTATION_LOST", self.lost_path)

    def patch(self, name, value):
        original = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(lambda: setattr(mod, name, original))

    def read_auth(self):
        return json.loads(self.auth_path.read_text(encoding="utf-8"))

    def write_auth(self, **token_overrides):
        auth = json.loads(json.dumps(AUTH))
        auth["tokens"].update(token_overrides)
        self.auth_path.write_text(json.dumps(auth), encoding="utf-8")


class CodexPersistRotationTest(CodexCredentialCase):
    def test_writes_rotation_and_preserves_unrelated_fields(self):
        mod.codex_persist_rotation("old-refresh", "new-access", "new-refresh")

        tokens = self.read_auth()["tokens"]
        self.assertEqual(tokens["access_token"], "new-access")
        self.assertEqual(tokens["refresh_token"], "new-refresh")
        self.assertEqual(tokens["account_id"], "acct-1")
        self.assertEqual(tokens["id_token"], "keep-me")

    def test_keeps_stored_refresh_when_server_returned_none(self):
        mod.codex_persist_rotation("old-refresh", "new-access", None)

        tokens = self.read_auth()["tokens"]
        self.assertEqual(tokens["access_token"], "new-access")
        self.assertEqual(tokens["refresh_token"], "old-refresh")

    def test_raises_when_auth_file_is_unreadable(self):
        self.auth_path.write_text("{ this is not json", encoding="utf-8")

        with self.assertRaises(mod.CodexPersistError):
            mod.codex_persist_rotation("old-refresh", "new-access", "new-refresh")

    def test_raises_when_auth_file_is_missing(self):
        self.auth_path.unlink()

        with self.assertRaises(mod.CodexPersistError):
            mod.codex_persist_rotation("old-refresh", "new-access", "new-refresh")

    def test_raises_when_the_write_itself_fails(self):
        self.patch("write_atomic", failing_write)

        with self.assertRaises(mod.CodexPersistError):
            mod.codex_persist_rotation("old-refresh", "new-access", "new-refresh")

    def test_concurrent_rotation_is_distinguishable_and_does_not_clobber(self):
        """Codex rotated the credential while our refresh was in flight. Its token
        is the live one, and this is not the same incident as a failed write."""
        self.write_auth(refresh_token="rotated-by-codex", access_token="codex-access")

        with self.assertRaises(mod.CodexConcurrentRotation):
            mod.codex_persist_rotation("old-refresh", "new-access", "new-refresh")

        self.assertEqual(self.read_auth()["tokens"]["refresh_token"], "rotated-by-codex")


class CodexRotationLossMarkerTest(CodexCredentialCase):
    def test_marker_records_a_fingerprint_not_the_token(self):
        mod.codex_record_rotation_loss("old-refresh")

        marker = json.loads(self.lost_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["consumedRefreshSha256"], mod.token_fingerprint("old-refresh"))
        self.assertNotIn("old-refresh", self.lost_path.read_text(encoding="utf-8"))

    def test_still_lost_while_the_dead_credential_is_stored(self):
        mod.codex_record_rotation_loss("old-refresh")

        self.assertTrue(mod.codex_rotation_still_lost("old-refresh"))

    def test_marker_clears_once_the_credential_changes(self):
        mod.codex_record_rotation_loss("old-refresh")
        self.patch("log", lambda _m: None)

        self.assertFalse(mod.codex_rotation_still_lost("relogged-in-refresh"))
        self.assertFalse(self.lost_path.exists())

    def test_no_marker_means_nothing_is_lost(self):
        self.assertFalse(mod.codex_rotation_still_lost("old-refresh"))


class CodexPollRotationTest(CodexCredentialCase):
    """What the poll does with each rotation outcome."""

    def setUp(self):
        super().setUp()
        self.logs = []
        self.fetched = []
        self.fallbacks = []

        self.patch("log", self.logs.append)
        # Force the "about to expire" branch so a refresh is attempted.
        self.patch("jwt_exp", lambda _token: mod.NOW_EPOCH + 1)
        self.patch("codex_refresh", lambda _rt: {"access_token": "new-access", "refresh_token": "new-refresh"})
        self.patch("http_get_json", self.record_fetch)
        self.patch("apply_reset_fallback", lambda *paths: self.fallbacks.append(paths))

    def record_fetch(self, url, headers, timeout=15):
        self.fetched.append(headers)
        raise urllib_error_stub()

    @property
    def log_text(self):
        return " ".join(self.logs)

    def test_stops_and_reports_when_rotation_cannot_be_saved(self):
        self.patch("write_atomic", failing_write)

        mod.codex_poll()

        self.assertEqual(self.fetched, [], "poll continued with an unpersisted token")
        self.assertNotIn("token refreshed", self.log_text, "reported success after losing the rotation")
        self.assertIn("NOT saved", self.log_text)
        self.assertIn("codex login", self.log_text)

    def cannot_persist(self):
        """The common shape of the failure: auth.json is unreadable at write-back
        time, while the disk itself is fine."""
        def raise_persist_error(*_args, **_kwargs):
            raise mod.CodexPersistError("auth.json is missing or unreadable as JSON")

        self.patch("codex_persist_rotation", raise_persist_error)

    def test_records_the_loss_so_later_runs_stay_loud(self):
        self.cannot_persist()

        mod.codex_poll()

        self.assertTrue(self.lost_path.exists(), "the lost rotation was not recorded")
        self.assertTrue(mod.codex_rotation_still_lost("old-refresh"))

    def test_a_lost_rotation_still_decays_the_card(self):
        self.cannot_persist()

        mod.codex_poll()

        self.assertEqual(len(self.fallbacks), 1, "the Card was left frozen instead of decaying")

    def test_a_total_write_failure_reports_even_though_it_cannot_record(self):
        """If the disk is gone we cannot leave a marker either. Say so rather than
        pretending the loss was recorded."""
        self.patch("write_atomic", failing_write)

        mod.codex_poll()

        self.assertIn("NOT saved", self.log_text)
        self.assertIn("could not record the lost rotation", self.log_text)
        self.assertEqual(self.fetched, [])

    def test_a_later_run_refuses_before_burning_another_refresh(self):
        refreshes = []
        mod.codex_record_rotation_loss("old-refresh")
        self.patch("codex_refresh", lambda rt: refreshes.append(rt))

        mod.codex_poll()

        self.assertEqual(refreshes, [], "tried to refresh with a credential known to be dead")
        self.assertEqual(self.fetched, [])
        self.assertIn("codex login", self.log_text)
        self.assertEqual(len(self.fallbacks), 1)

    def test_concurrent_rotation_carries_on_with_the_stored_token(self):
        """Codex rotated it for us. Nothing is broken, so the poll must not abort
        and must not tell a healthy user to re-login."""
        original_persist = mod.codex_persist_rotation

        def rotate_then_persist(expected, new_access, new_refresh):
            self.write_auth(refresh_token="rotated-by-codex", access_token="codex-access")
            return original_persist(expected, new_access, new_refresh)

        self.patch("codex_persist_rotation", rotate_then_persist)

        mod.codex_poll()

        self.assertEqual(len(self.fetched), 1, "poll aborted on a benign concurrent rotation")
        self.assertEqual(self.fetched[0]["Authorization"], "Bearer codex-access")
        self.assertNotIn("codex login", self.log_text, "told a healthy user to re-login")
        self.assertFalse(self.lost_path.exists(), "recorded a loss that did not happen")

    def test_a_non_dict_refresh_response_degrades_instead_of_exploding(self):
        self.patch("codex_refresh", lambda _rt: ["not", "a", "dict"])

        mod.codex_poll()

        self.assertEqual(len(self.fetched), 1, "gave up instead of trying the existing token")
        self.assertEqual(self.fetched[0]["Authorization"], "Bearer old-access")
        self.assertIn("unexpected shape", self.log_text)


def urllib_error_stub():
    """Stop the poll after the usage request without pretending it succeeded."""
    return RuntimeError("usage fetch stubbed out")


if __name__ == "__main__":
    unittest.main()
