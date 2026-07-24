"""Codex joins the Usage Reading.

Its response maps into the same vocabulary Claude's does, so its Card, what it
keeps and how it recovers all behave the same way. What it gains on the way is
the Reset validation it never had: Codex states a Window by its length and its
Reset as a bare epoch from an undocumented endpoint, so a value on the wrong
scale used to be persisted unchallenged and could later read every Window at 0%
while the user was in fact near their limit.

`RESPONSE` is a live response with the account's identifiers removed. Note that
it carries `reset_after_seconds` beside `reset_at` — the relative form of the
same moment, and exactly what a wrong-unit Reset would look like.
"""

import json
import tempfile
import unittest
from pathlib import Path

import runcat_poll as mod  # the tests package puts the repo root on sys.path

NOW = 1784806000.0        # 2026-07-23T11:26:40Z
WEEKLY_RESET = 1785283618  # 5d 12h past NOW
HOUR = 3600.0
DAY = 86400.0

RESPONSE = {
    "plan_type": "prolite",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 13,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 477230,
            "reset_at": WEEKLY_RESET,
        },
        "secondary_window": None,
    },
}

LIVE_CARD = {
    "title": "Codex Pro 5x",
    "symbol": "camera.aperture",
    "metrics": [
        {"title": "주간 한도", "formattedValue": "13%", "normalizedValue": 0.13},
        {"title": "재설정", "formattedValue": "5일 12시간 후"},
    ],
    "lastUpdatedDate": "2026-07-23T11:26:40Z",
    "metricsBarValue": "13%",
}

AUTH = {"tokens": {"access_token": "access", "refresh_token": "refresh", "account_id": "acct-1"}}


def both_windows(session_used=47, weekly_used=13, **overrides):
    """A response with a Session Window as well, which this account does not have."""
    session = {"used_percent": session_used, "limit_window_seconds": 18000,
               "reset_at": NOW + 2 * HOUR}
    session.update(overrides)
    return {"plan_type": "prolite", "rate_limit": {
        "primary_window": session,
        "secondary_window": {"used_percent": weekly_used, "limit_window_seconds": 604800,
                             "reset_at": WEEKLY_RESET},
    }}


class CodexCase(unittest.TestCase):
    """A temporary ~/.codex, with the clock and the poll's inputs under control."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)

        self.auth_path = home / "auth.json"
        self.auth_path.write_text(json.dumps(AUTH), encoding="utf-8")
        self.card_path = home / "runcat-usage.json"
        self.reading_path = home / "runcat-reading.json"
        self.logs = []

        self.patch("CODEX_AUTH_FILE", self.auth_path)
        self.patch("CODEX_CARD", self.card_path)
        self.patch("CODEX_READING", self.reading_path)
        self.patch("CODEX_ROTATION_LOST", home / "runcat-rotation-lost.json")
        self.patch("log", self.logs.append)
        mod.interface_lang.cache_clear()
        self.addCleanup(mod.interface_lang.cache_clear)
        self.speak("ko")

    def patch(self, name, value):
        original = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(lambda: setattr(mod, name, original))

    def speak(self, lang):
        self.patch("detect_lang", lambda: lang)
        mod.interface_lang.cache_clear()

    @property
    def log_text(self):
        return " ".join(self.logs)

    def poll(self, response=RESPONSE, http_fails=False, now=NOW):
        """A Codex poll at `now`, with everything outside the poller stubbed. The
        token-expiry stub is derived from that same `now` so moving the clock never
        trips the refresh branch — the stub no longer has to be kept in lock-step
        with a patched global the way the old `at()` helper did."""
        self.patch("jwt_exp", lambda _token: now + 30 * DAY)  # never refresh at `now`

        def fetch(url, headers, timeout=15):
            if http_fails:
                raise RuntimeError("the usage endpoint is unreachable")
            return 200, response

        self.patch("http_get_json", fetch)
        mod.codex_poll(now)

    def reading(self, response=RESPONSE, now=None):
        return mod.codex_reading(response, mod.codex_plan_label(response.get("plan_type")),
                                 NOW if now is None else now)

    def card(self):
        return json.loads(self.card_path.read_text(encoding="utf-8"))

    def rows(self):
        return [(row["title"], row["formattedValue"]) for row in self.card()["metrics"]]

    def stored(self):
        return json.loads(self.reading_path.read_text(encoding="utf-8"))


class CodexReadingTest(CodexCase):
    def test_a_window_is_identified_by_its_length_never_by_anything_on_screen(self):
        ids = [w.id for w in self.reading(both_windows()).windows]

        self.assertEqual(ids, ["session", "weekly"])

    def test_codex_and_claude_call_the_same_window_the_same_thing(self):
        self.assertEqual(self.reading().windows[0].id, mod.WEEKLY_WINDOW)

    def test_an_unfamiliar_window_length_keeps_its_length_as_its_identity(self):
        response = both_windows(limit_window_seconds=432000)  # five days

        session = self.reading(response).windows[0]

        self.assertEqual(session.id, "window_length:432000")
        self.assertEqual(session.label, "5d", "it still has to show up somewhere")

    def test_an_unfamiliar_length_is_labelled_in_the_largest_unit_that_divides_it(self):
        for seconds, label in ((432000, "5d"), (10800, "3h"), (1200, "20m")):
            with self.subTest(seconds=seconds):
                window = self.reading(both_windows(limit_window_seconds=seconds)).windows[0]
                self.assertEqual(window.label, label)

    def test_a_window_with_no_length_has_no_identity_to_carry(self):
        response = both_windows()
        response["rate_limit"]["primary_window"].pop("limit_window_seconds")

        self.assertEqual([w.id for w in self.reading(response).windows], ["weekly"])

    def test_a_window_without_a_numeric_share_is_left_out(self):
        response = both_windows(used_percent=None)

        self.assertEqual([w.id for w in self.reading(response).windows], ["weekly"])

    def test_a_missing_window_is_simply_absent(self):
        """This account has no Session Window at all — `secondary_window` is null."""
        self.assertEqual([w.id for w in self.reading().windows], ["weekly"])

    def test_the_reading_records_the_provider_and_the_plan(self):
        reading = self.reading()

        self.assertEqual(reading.provider, "codex")
        self.assertEqual(reading.plan, "Pro 5x")
        self.assertEqual(reading.captured_at, NOW)


class CodexResetValidationTest(CodexCase):
    def test_a_reset_in_milliseconds_is_discarded(self):
        window = self.reading(both_windows(reset_at=(NOW + HOUR) * 1000)).windows[0]

        self.assertEqual(window.used, 47.0, "the Window itself must survive")
        self.assertIsNone(window.resets_at)
        self.assertIn("plausible", self.log_text)

    def test_a_relative_reset_is_discarded(self):
        """`reset_after_seconds` is right there beside `reset_at` in the response;
        reading the wrong one lands the moment in 1970."""
        window = self.reading(both_windows(reset_at=477230)).windows[0]

        self.assertIsNone(window.resets_at)

    def test_a_reset_that_is_not_a_number_is_discarded(self):
        window = self.reading(both_windows(reset_at="soon")).windows[0]

        self.assertIsNone(window.resets_at)

    def test_a_plausible_reset_survives(self):
        self.assertEqual(self.reading().windows[0].resets_at, WEEKLY_RESET)

    def test_a_discarded_reset_is_never_persisted(self):
        """This is what the validation is for: an unbelievable Reset used to be
        written down, and a later outage would then read every Window at 0%."""
        self.poll(both_windows(reset_at=(NOW + HOUR) * 1000))

        self.assertIsNone(self.stored()["windows"][0]["resetsAt"])

        self.poll(http_fails=True, now=NOW + 10 * DAY)

        self.assertEqual(self.rows()[0], ("현재 세션", "47%\n"),
                         "a Window with no believable Reset must never decay")


class CodexCardTest(CodexCase):
    def test_renders_exactly_the_card_the_poller_wrote(self):
        self.poll()

        self.assertEqual(self.card(), LIVE_CARD)

    def test_renders_the_same_window_in_english(self):
        self.speak("en")

        self.poll()

        self.assertEqual(self.rows(), [("Weekly", "13%"), ("reset", "5d 12h")])

    def test_two_windows_are_spaced_apart_the_way_claudes_are(self):
        self.poll(both_windows())

        self.assertEqual(self.rows(), [
            ("현재 세션", "47%"),
            ("재설정", "2시간 0분 후\n"),
            ("주간 한도", "13%"),
            ("재설정", "5일 12시간 후"),
        ])

    def test_the_menu_bar_carries_the_session_and_weekly_windows(self):
        self.poll(both_windows())

        self.assertEqual(self.card()["metricsBarValue"], "47% · 13%")


class CodexRecoveryTest(CodexCase):
    def test_a_successful_poll_keeps_the_reading(self):
        self.poll()

        self.assertEqual(self.stored()["provider"], "codex")
        self.assertEqual([w["id"] for w in self.stored()["windows"]], ["weekly"])

    def test_recovery_rebuilds_the_card_from_the_decayed_stale_reading(self):
        self.poll(both_windows())

        # `now` is past the Session Window's Reset, not the Weekly one.
        self.poll(http_fails=True, now=NOW + 3 * HOUR)

        self.assertEqual(self.rows(), [
            ("현재 세션", "0%\n"),
            ("주간 한도", "13%"),
            ("재설정", "5일 9시간 후"),
        ])

    def test_recovery_still_works_after_the_interface_language_changes(self):
        self.poll(both_windows())
        self.speak("en")

        self.poll(http_fails=True, now=NOW + 3 * HOUR)

        self.assertEqual(self.rows()[0], ("Session", "0%\n"))

    def test_the_obsolete_reset_state_file_is_treated_as_absent(self):
        """It is `{label: epoch}` with no `windows`, so it can never be mistaken
        for a Reading — the Card is left standing instead."""
        self.poll()
        published = self.card()
        self.reading_path.write_text(json.dumps({"주간 한도": WEEKLY_RESET}), encoding="utf-8")

        self.poll(http_fails=True)

        self.assertEqual(self.card(), published)
        self.assertIn("keeping last-good", self.log_text)

    def test_a_reset_that_fails_validation_keeps_the_one_already_persisted(self):
        """`both_windows` overrides the Session Window, so that is the one whose
        Reset must be carried — asserting on the Weekly one would pass on the
        value the new response supplied anyway."""
        self.poll(both_windows())

        self.poll(both_windows(reset_at="whenever"))

        session = self.stored()["windows"][0]
        self.assertEqual(session["id"], "session")
        self.assertEqual(session["resetsAt"], NOW + 2 * HOUR)

    def test_a_response_with_no_windows_leaves_the_card_and_the_reading_alone(self):
        """Nothing to publish must not blank the Card or destroy the record."""
        self.poll(both_windows())
        published, kept = self.card(), self.stored()

        self.poll({"plan_type": "prolite"})   # no rate_limit at all

        self.assertEqual(self.card(), published)
        self.assertEqual(self.stored(), kept)
        self.assertIn("keeping last-good", self.log_text)

    def test_no_credential_at_all_recovers_rather_than_freezing_the_card(self):
        """`codex logout`, or an auth.json that loses its token. The Card must
        decay from what we last knew, the way Claude's does when its token
        expires — not stand at a share that may have reset hours ago."""
        self.poll(both_windows())
        self.auth_path.write_text(json.dumps({"tokens": {}}), encoding="utf-8")

        self.poll(now=NOW + 3 * HOUR)

        self.assertEqual(self.rows()[0], ("현재 세션", "0%\n"))
        self.assertIn("no access token", self.log_text)

    def test_a_lost_rotation_recovers_rather_than_leaving_the_card_frozen(self):
        self.poll(both_windows())
        mod.codex_record_rotation_loss("refresh")

        self.poll(now=NOW + 3 * HOUR)

        self.assertIn("codex login", self.log_text)
        self.assertEqual(self.rows()[0], ("현재 세션", "0%\n"))


if __name__ == "__main__":
    unittest.main()
