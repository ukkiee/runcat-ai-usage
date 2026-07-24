"""What the poller keeps, and what it rebuilds from when it cannot poll.

The Usage Reading is the record now: the Card is derived from it, and a poll that
cannot happen rebuilds the Card from a decayed Stale Reading instead of patching
what was already published. Two defects die with that, and both are pinned here —
the blank line between Windows used to be destroyed permanently by the first
failed poll, and recovery stopped working entirely the moment the interface
language changed, because the state was keyed by the rendered row title.
"""

import json
import tempfile
import unittest
from pathlib import Path

import runcat_poll as mod  # the tests package puts the repo root on sys.path

NOW = 1784794645.0           # 2026-07-23T08:17:25Z
HOUR = 3600.0
DAY = 86400.0

RESPONSE = {
    "limits": [
        {"kind": "session", "percent": 47, "resets_at": "2026-07-23T10:20:00Z"},   # NOW + 2h
        {"kind": "weekly_all", "percent": 77, "resets_at": "2026-07-26T19:00:00Z"},  # NOW + 3d
        {
            "kind": "weekly_scoped",
            "percent": 66,
            "resets_at": "2026-07-26T18:59:59Z",
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        },
    ],
}
SESSION_RESET = 1784802000.0
WEEKLY_RESET = 1785092400.0


def reading(*windows, provider="claude", plan="Max 20x", captured_at=NOW):
    return mod.UsageReading(provider=provider, plan=plan,
                            windows=tuple(windows), captured_at=captured_at)


def window(window_id=None, used=47.0, resets_at=None, label=""):
    return mod.Window(id=window_id or mod.SESSION_WINDOW, used=used,
                      resets_at=resets_at, label=label)


class StaleReadingCase(unittest.TestCase):
    """A temporary ~/.claude, with the clock and the poll's inputs under control."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)

        self.card_path = home / "runcat-usage.json"
        self.reading_path = home / "runcat-reading.json"
        self.logs = []

        self.patch("CLAUDE_CARD", self.card_path)
        self.patch("CLAUDE_READING", self.reading_path)
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

    def poll(self, response=RESPONSE, token_expired=False, http_fails=False, now=NOW):
        """A Claude poll at `now`, with everything outside the poller stubbed.

        The moment enters as an argument — the poller no longer reads it from a
        module global — so the token-expiry stub is derived from that same `now`
        rather than from a patched clock kept in sync with it."""
        expires_at = (now * 1000 - 1) if token_expired else (now * 1000 + 60_000)
        self.patch("claude_read_token", lambda: {
            "accessToken": "x", "expiresAt": expires_at,
            "rateLimitTier": "default_claude_max_20x",
        })

        def fetch(url, headers, timeout=15):
            if http_fails:
                raise RuntimeError("the usage endpoint is unreachable")
            return 200, response

        self.patch("http_get_json", fetch)
        mod.claude_poll(now)

    def card(self):
        return json.loads(self.card_path.read_text(encoding="utf-8"))

    def rows(self):
        return [(row["title"], row["formattedValue"]) for row in self.card()["metrics"]]

    def stored(self):
        return json.loads(self.reading_path.read_text(encoding="utf-8"))


class PersistenceTest(StaleReadingCase):
    def test_a_reading_survives_a_round_trip(self):
        original = reading(
            window(mod.SESSION_WINDOW, 47.0, SESSION_RESET),
            window(mod.scoped_window_id("Fable"), 66.0, WEEKLY_RESET, label="Fable"),
            # Deliberately not the current clock: what goes to disk is the moment
            # the Reading was captured, which is what decay measures against.
            captured_at=NOW - DAY,
        )

        restored = mod.reading_from_json(mod.reading_as_json(original))

        self.assertEqual(restored, original)

    def test_a_reading_is_persisted_by_identity_never_by_label(self):
        self.poll()

        stored = self.stored()
        self.assertEqual([w["id"] for w in stored["windows"]],
                         ["session", "weekly", "weekly_scoped:Fable"])
        for window_id in ("현재 세션", "주간 한도", "재설정"):
            self.assertNotIn(window_id, json.dumps(stored, ensure_ascii=False))

    def test_the_obsolete_reset_state_file_reads_as_absent(self):
        """`{label: epoch}` has no `windows`, so it is not mistaken for a Reading
        with nothing in it — the worst case is one cycle without a Stale Reading."""
        self.assertIsNone(mod.reading_from_json({"현재 세션": 1784802000}))

    def test_something_that_is_not_a_reading_reads_as_absent(self):
        rows = [{"id": "session", "used": 47}]
        for data in (None, [], "", {}, {"windows": {}}, {"windows": []},
                     {"windows": rows},                                  # no capturedAt
                     {"windows": rows, "capturedAt": NOW},               # no provider
                     {"windows": rows, "capturedAt": NOW, "provider": "runcat"},
                     # A provider we know, but `windows` is not a list of Windows.
                     {"provider": "claude", "capturedAt": NOW},
                     {"provider": "claude", "capturedAt": NOW, "windows": 5},
                     {"provider": "claude", "capturedAt": NOW, "windows": "session"},
                     {"provider": "claude", "capturedAt": NOW, "windows": [None, 7]},
                     {"provider": "claude", "capturedAt": "recently", "windows": rows},
                     # A provider that cannot even be looked up: rejected, never raised.
                     {"provider": [], "capturedAt": NOW, "windows": rows},
                     {"provider": {"name": "claude"}, "capturedAt": NOW, "windows": rows}):
            with self.subTest(data=data):
                self.assertIsNone(mod.reading_from_json(data))

    def test_a_state_file_we_cannot_read_at_all_is_absent_rather_than_fatal(self):
        """A file that is not UTF-8 must not take the poll down with it. Nothing
        here writes one, but a poll that dies before publishing the Card leaves it
        frozen on every later run too, and only deleting the file would recover."""
        self.reading_path.write_bytes(b"\xff\xfe not text at all")

        self.assertIsNone(mod.load_json(self.reading_path))

        self.poll()  # a live poll must still publish over it

        self.assertEqual(self.rows()[0][0], "현재 세션")

    def test_a_window_without_a_usable_share_is_left_out_on_the_way_back_in(self):
        restored = mod.reading_from_json({
            "provider": "claude", "plan": "Max 20x", "capturedAt": NOW,
            "windows": [{"id": "session", "used": "lots"},
                        {"id": "weekly", "used": 77.0, "resetsAt": WEEKLY_RESET}],
        })

        self.assertEqual([w.id for w in restored.windows], ["weekly"])

    def test_a_persisted_reset_is_not_put_back_through_the_plausibility_window(self):
        """It was checked when it was written. An outage long enough to push it
        more than a day into the past is exactly what decay exists for — dropping
        it here would throw away the state recovery needs."""
        long_past = NOW - 9 * DAY

        restored = mod.reading_from_json({
            "provider": "claude", "plan": "Max 20x", "capturedAt": long_past,
            "windows": [{"id": "session", "used": 47.0, "resetsAt": long_past + HOUR}],
        })

        self.assertEqual(restored.windows[0].resets_at, long_past + HOUR)


class DecayTest(StaleReadingCase):
    def test_a_window_whose_reset_has_passed_reads_zero_and_loses_its_countdown(self):
        stale = reading(window(mod.SESSION_WINDOW, 47.0, NOW + HOUR))

        decayed = mod.decay(stale, NOW + 2 * HOUR)

        self.assertEqual(decayed.windows[0].used, 0.0)
        self.assertIsNone(decayed.windows[0].resets_at)

    def test_a_window_that_has_not_reset_yet_is_untouched(self):
        stale = reading(window(mod.SESSION_WINDOW, 47.0, NOW + 2 * HOUR))

        decayed = mod.decay(stale, NOW + HOUR)

        self.assertEqual(decayed.windows[0], stale.windows[0])

    def test_a_window_with_no_reset_is_untouched(self):
        stale = reading(window(mod.SESSION_WINDOW, 47.0, None))

        self.assertEqual(mod.decay(stale, NOW + 30 * DAY), stale)

    def test_decaying_an_already_decayed_reading_changes_nothing(self):
        stale = reading(window(mod.SESSION_WINDOW, 47.0, NOW + HOUR),
                        window(mod.WEEKLY_WINDOW, 77.0, NOW + 3 * DAY))

        once = mod.decay(stale, NOW + 2 * HOUR)
        twice = mod.decay(once, NOW + 2 * HOUR)

        self.assertEqual(twice, once)

    def test_a_reset_that_had_already_passed_when_the_reading_was_captured_is_left_alone(self):
        """The share was measured after that Reset, so it is already the new
        Window's. Zeroing it would underreport usage — the one failure this
        project treats as worse than a stale Card."""
        stale = reading(window(mod.SESSION_WINDOW, 47.0, NOW - HOUR), captured_at=NOW)

        decayed = mod.decay(stale, NOW + HOUR)

        self.assertEqual(decayed.windows[0].used, 47.0)

    def test_the_labels_and_identities_survive_decay(self):
        stale = reading(window(mod.scoped_window_id("Fable"), 66.0, NOW + HOUR, label="Fable"))

        decayed = mod.decay(stale, NOW + 2 * HOUR)

        self.assertEqual(decayed.windows[0].id, "weekly_scoped:Fable")
        self.assertEqual(decayed.windows[0].label, "Fable")


class CarryForwardTest(StaleReadingCase):
    def malformed_reset_response(self):
        limits = [dict(limit) for limit in RESPONSE["limits"]]
        limits[0]["resets_at"] = "whenever"
        return {"limits": limits}

    def test_a_reset_that_fails_validation_keeps_the_one_already_persisted(self):
        self.poll()

        self.poll(self.malformed_reset_response())

        session = self.stored()["windows"][0]
        self.assertEqual(session["id"], "session")
        self.assertEqual(session["resetsAt"], SESSION_RESET)

    def test_an_outage_that_runs_past_the_carried_reset_still_decays_the_window(self):
        """The whole point of carrying it: one transient glitch must not erase the
        only state a later outage could have decayed from."""
        self.poll()
        self.poll(self.malformed_reset_response())

        self.poll(http_fails=True, now=SESSION_RESET + HOUR)

        self.assertEqual(self.rows()[0], ("현재 세션", "0%\n"))

    def test_a_carried_reset_that_has_already_passed_is_not_carried(self):
        """A Reset that has passed says nothing about the next one, and carrying it
        would decay a share we have only just measured."""
        self.poll()

        self.poll(self.malformed_reset_response(), now=SESSION_RESET + HOUR)

        self.assertIsNone(self.stored()["windows"][0]["resetsAt"])

    def test_used_shares_always_come_from_the_new_response(self):
        self.poll()

        louder = {"limits": [dict(RESPONSE["limits"][0], percent=91)]}
        self.poll(louder)

        self.assertEqual(self.stored()["windows"][0]["used"], 91.0)

    def test_a_window_with_no_reset_from_either_source_simply_has_none(self):
        self.poll(self.malformed_reset_response())

        self.assertIsNone(self.stored()["windows"][0]["resetsAt"])


class LegacyShapeTest(StaleReadingCase):
    """What the poller does when the primary response shape is gone."""

    def legacy(self, utilization=47.0):
        """The older top-level fields, with no `limits[]` at all."""
        return {
            "five_hour": {"utilization": utilization, "resets_at": "2026-07-23T10:20:00Z"},
            "seven_day": {"utilization": 77.0, "resets_at": "2026-07-26T19:00:00Z"},
        }

    def test_the_card_keeps_working_when_the_primary_shape_disappears(self):
        self.poll(self.legacy())

        self.assertEqual(self.rows(), [
            ("현재 세션", "47%"),
            ("재설정", "2시간 2분 후\n"),
            ("주간 한도", "77%"),
            ("재설정", "3일 10시간 후"),
        ])

    def test_a_share_that_contradicts_the_scale_leaves_the_previous_reading_standing(self):
        """Silently underreporting usage at the very moment the safety net is
        first used is worse than showing a stale Card."""
        self.poll()
        published, kept = self.card(), self.stored()

        self.poll(self.legacy(utilization=140.0))

        self.assertEqual(self.card(), published)
        self.assertEqual(self.stored(), kept)
        self.assertIn("nothing is published", self.log_text)


class RecoveryTest(StaleReadingCase):
    def test_a_successful_poll_persists_the_reading_beside_the_card(self):
        self.poll()

        self.assertTrue(self.reading_path.exists())
        self.assertEqual(self.stored()["provider"], "claude")
        self.assertEqual(self.stored()["capturedAt"], NOW)

    def test_recovery_rebuilds_the_card_with_the_spacing_between_windows_intact(self):
        """The spacer is a trailing newline smuggled into the previous row's value.
        Patching the published Card in place overwrote it, and it never came back
        until a live poll rebuilt everything. Rebuilding cannot lose it."""
        self.poll()

        self.poll(http_fails=True, now=SESSION_RESET + HOUR)

        self.assertEqual(self.rows(), [
            # The decayed Window loses its countdown and takes over the spacer.
            ("현재 세션", "0%\n"),
            ("주간 한도", "77%"),
            ("재설정", "3일 7시간 후\n"),
            ("Fable", "66%"),
            ("재설정", "3일 7시간 후"),
        ])

    def test_the_spacing_survives_however_many_failed_polls_follow(self):
        late = SESSION_RESET + HOUR
        self.poll()
        self.poll(http_fails=True, now=late)
        first = self.rows()

        self.poll(http_fails=True, now=late)
        self.poll(token_expired=True, now=late)

        self.assertEqual(self.rows(), first)

    def test_recovery_still_works_after_the_interface_language_changes(self):
        """The old state was keyed by the rendered row title, so every lookup
        missed once the Card was rebuilt in another language — and it self-healed
        only on a successful poll, which is exactly what is not happening."""
        self.poll()
        self.speak("en")

        self.poll(http_fails=True, now=SESSION_RESET + HOUR)

        self.assertEqual(self.rows()[0], ("Session", "0%\n"))
        self.assertEqual(self.rows()[1], ("Weekly", "77%"))

    def test_recovery_keeps_counting_down_the_windows_that_have_not_reset(self):
        self.poll()

        self.poll(http_fails=True, now=NOW + HOUR)

        self.assertEqual(self.rows()[1], ("재설정", "1시간 2분 후\n"))

    def test_an_expired_token_recovers_the_same_way_an_unreachable_endpoint_does(self):
        self.poll()

        self.poll(token_expired=True, now=SESSION_RESET + HOUR)

        self.assertEqual(self.rows()[0], ("현재 세션", "0%\n"))

    def test_recovery_without_a_reading_leaves_the_published_card_alone(self):
        self.poll()
        published = self.card()
        self.reading_path.unlink()

        self.poll(http_fails=True)

        self.assertEqual(self.card(), published)
        self.assertIn("keeping last-good", self.log_text)

    def test_a_failed_poll_never_rewrites_the_reading(self):
        self.poll()
        stored = self.stored()

        self.poll(http_fails=True, now=SESSION_RESET + HOUR)

        self.assertEqual(self.stored(), stored, "recovery must not overwrite the record")


if __name__ == "__main__":
    unittest.main()
