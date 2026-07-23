"""The Claude Card is now downstream of a Usage Reading.

Claude's response becomes a provider-neutral Reading first — every Window
carrying a stable identity, a bounded used share and a Reset we can believe —
and the Card is rendered from that Reading with the clock and the label set
handed in rather than read from module state.

These tests pin both halves: what the mapping keeps, and that the rendering
still produces exactly the Card the poller wrote before the change. The fixtures
are real — `LIVE_CARD` is the Card observed in `~/.claude/runcat-usage.json`,
and `NOW` is the moment it was written.
"""

import subprocess
import sys
import unittest
from pathlib import Path

import runcat_poll as mod  # the tests package puts the repo root on sys.path

REPO = Path(__file__).resolve().parent.parent

NOW = 1784794645.0           # 2026-07-23T08:17:25Z — when LIVE_CARD was written
SESSION_RESET = 1784802000   # 2026-07-23T10:20:00Z — 2h 2m past NOW
WEEKLY_RESET = 1785092400    # 2026-07-26T19:00:00Z — 3d 10h past NOW
SCOPED_RESET = 1785092399    # a second earlier, as the live response reports it

LIVE_RESPONSE = {
    "limits": [
        {"kind": "session", "percent": 47, "resets_at": "2026-07-23T10:20:00Z"},
        {"kind": "weekly_all", "percent": 77, "resets_at": "2026-07-26T19:00:00Z"},
        {
            "kind": "weekly_scoped",
            "percent": 66,
            "resets_at": "2026-07-26T18:59:59Z",
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        },
    ],
    # The live response carries the older per-Window fields alongside `limits[]`.
    "five_hour": {"utilization": 47.0, "resets_at": "2026-07-23T10:20:00Z"},
    "seven_day": {"utilization": 77.0, "resets_at": "2026-07-26T19:00:00Z"},
}

LIVE_CARD = {
    "title": "Claude Code Max 20x",
    "symbol": "staroflife",
    "metrics": [
        {"title": "현재 세션", "formattedValue": "47%", "normalizedValue": 0.47},
        {"title": "재설정", "formattedValue": "2시간 2분 후\n"},
        {"title": "주간 한도", "formattedValue": "77%", "normalizedValue": 0.77},
        {"title": "재설정", "formattedValue": "3일 10시간 후\n"},
        {"title": "Fable", "formattedValue": "66%", "normalizedValue": 0.66},
        {"title": "재설정", "formattedValue": "3일 10시간 후"},
    ],
    "lastUpdatedDate": "2026-07-23T08:17:25Z",
    "metricsBarValue": "47% · 77%",
}


class ReadingCase(unittest.TestCase):
    """Captures what the poller logs, so a clamp or a discard can be asserted on."""

    def setUp(self):
        self.logs = []
        self.patch("log", self.logs.append)
        # The interface language is cached for the run; no test may inherit it.
        mod.interface_lang.cache_clear()
        self.addCleanup(mod.interface_lang.cache_clear)

    def patch(self, name, value):
        original = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(lambda: setattr(mod, name, original))

    @property
    def log_text(self):
        return " ".join(self.logs)

    def reading(self, body=None, plan="Max 20x", now=NOW):
        return mod.claude_reading(body if body is not None else LIVE_RESPONSE, plan, now)

    def one_window(self, **overrides):
        """A Reading built from a single Session limit, for the boundary cases."""
        limit = {"kind": "session", "percent": 47, "resets_at": "2026-07-23T10:20:00Z"}
        limit.update(overrides)
        return self.reading({"limits": [limit]})


class ClaudeReadingTest(ReadingCase):
    def test_windows_carry_stable_identities(self):
        ids = [window.id for window in self.reading().windows]

        self.assertEqual(ids, ["session", "weekly", "weekly_scoped:Fable"])

    def test_no_identity_contains_a_display_label(self):
        """The whole point of the identity: it is the same string in any language."""
        for window in self.reading().windows:
            for labels in (mod.label_set("ko"), mod.label_set("en")):
                for display in labels.values():
                    self.assertNotIn(display, window.id)

    def test_a_model_scoped_window_carries_the_model_name_as_its_label(self):
        scoped = self.reading().windows[2]

        self.assertEqual(scoped.label, "Fable")
        self.assertEqual(scoped.id, mod.scoped_window_id("Fable"))

    def test_used_shares_and_resets_survive_the_mapping(self):
        session, weekly, scoped = self.reading().windows

        self.assertEqual((session.used, weekly.used, scoped.used), (47.0, 77.0, 66.0))
        self.assertEqual(session.resets_at, SESSION_RESET)
        self.assertEqual(weekly.resets_at, WEEKLY_RESET)
        self.assertEqual(scoped.resets_at, SCOPED_RESET)

    def test_the_reading_records_the_provider_plan_and_moment(self):
        reading = self.reading()

        self.assertEqual(reading.provider, "claude")
        self.assertEqual(reading.plan, "Max 20x")
        self.assertEqual(reading.captured_at, NOW)

    def test_a_window_with_no_numeric_share_is_left_out(self):
        reading = self.reading({"limits": [
            {"kind": "session", "percent": None, "resets_at": "2026-07-23T10:20:00Z"},
            {"kind": "weekly_all", "percent": 77, "resets_at": "2026-07-26T19:00:00Z"},
        ]})

        self.assertEqual([w.id for w in reading.windows], ["weekly"])

    def test_an_unknown_kind_is_ignored_rather_than_guessed_at(self):
        reading = self.reading({"limits": [
            {"kind": "monthly_all", "percent": 12, "resets_at": "2026-07-26T19:00:00Z"},
            {"kind": "session", "percent": 47, "resets_at": "2026-07-23T10:20:00Z"},
        ]})

        self.assertEqual([w.id for w in reading.windows], ["session"])

    def test_a_scoped_window_without_a_model_name_has_no_identity_to_use(self):
        """`scope.model.id` is null in the live response, so the display name is the
        only handle there is. Without it the Window would collide with every other
        scoped one, which silently merges two different limits."""
        reading = self.reading({"limits": [
            {"kind": "weekly_scoped", "percent": 66, "resets_at": "2026-07-26T19:00:00Z"},
        ]})

        self.assertEqual(reading.windows, ())

    def test_two_scoped_windows_stay_apart(self):
        reading = self.reading({"limits": [
            {"kind": "weekly_scoped", "percent": 66, "resets_at": "2026-07-26T19:00:00Z",
             "scope": {"model": {"display_name": "Fable"}}},
            {"kind": "weekly_scoped", "percent": 12, "resets_at": "2026-07-26T19:00:00Z",
             "scope": {"model": {"display_name": "Opus"}}},
        ]})

        self.assertEqual([w.id for w in reading.windows],
                         ["weekly_scoped:Fable", "weekly_scoped:Opus"])

    def test_a_response_with_no_limits_maps_to_no_windows(self):
        self.assertEqual(self.reading({"limits": []}).windows, ())


class BoundaryValidationTest(ReadingCase):
    """The Reading is the trusted artifact now, so bad values must not get in."""

    def test_a_used_share_above_100_is_clamped_and_said_out_loud(self):
        window = self.one_window(percent=140).windows[0]

        self.assertEqual(window.used, 100.0)
        self.assertIn("clamped", self.log_text)

    def test_a_negative_used_share_is_clamped_and_said_out_loud(self):
        window = self.one_window(percent=-3).windows[0]

        self.assertEqual(window.used, 0.0)
        self.assertIn("clamped", self.log_text)

    def test_a_share_inside_the_range_passes_silently(self):
        window = self.one_window(percent=47).windows[0]

        self.assertEqual(window.used, 47.0)
        self.assertEqual(self.logs, [])

    def test_a_reset_in_milliseconds_is_discarded(self):
        """A millisecond epoch lands tens of thousands of years out."""
        self.assertIsNone(mod.plausible_reset(NOW * 1000, NOW, mod.SESSION_WINDOW))

    def test_a_relative_reset_is_discarded(self):
        """Seconds-until-reset rather than a moment lands in 1970."""
        self.assertIsNone(mod.plausible_reset(18000, NOW, mod.SESSION_WINDOW))

    def test_a_reset_that_is_not_a_number_is_discarded(self):
        for value in ("soon", None, True, [SESSION_RESET], float("nan")):
            with self.subTest(value=value):
                self.assertIsNone(mod.plausible_reset(value, NOW, mod.SESSION_WINDOW))

    def test_a_plausible_reset_is_kept(self):
        for value in (SESSION_RESET, WEEKLY_RESET, NOW - 3600, NOW + 29 * 86400):
            with self.subTest(value=value):
                self.assertEqual(mod.plausible_reset(value, NOW, mod.SESSION_WINDOW), float(value))

    def test_a_reset_claude_cannot_state_leaves_the_window_without_a_countdown(self):
        """Every way a Reset can be unusable, end to end: the wrong unit, a
        relative offset rather than a moment, and something that is not a number
        at all. The Window keeps its used share; only the countdown goes."""
        for description, value in (("milliseconds", SESSION_RESET * 1000),
                                   ("relative seconds", 18000),
                                   ("not a number", "in a bit")):
            with self.subTest(reset=description):
                window = self.one_window(resets_at=value).windows[0]

                self.assertEqual(window.used, 47.0)
                self.assertIsNone(window.resets_at)

    def test_an_unreadable_reset_leaves_the_window_but_drops_its_countdown(self):
        window = self.one_window(resets_at="whenever").windows[0]

        self.assertEqual(window.used, 47.0, "the Window itself must survive")
        self.assertIsNone(window.resets_at)
        self.assertIn("whenever", self.log_text)

    def test_an_implausible_reset_leaves_the_window_but_drops_its_countdown(self):
        window = self.one_window(resets_at="3026-07-23T10:20:00Z").windows[0]

        self.assertEqual(window.used, 47.0)
        self.assertIsNone(window.resets_at)
        self.assertIn("discard", self.log_text.lower())

    def test_a_window_with_no_countdown_renders_as_a_single_row(self):
        reading = self.one_window(resets_at="whenever")

        card = mod.render_card(reading, mod.label_set("ko"), NOW)

        self.assertEqual([row["title"] for row in card["metrics"]], ["현재 세션"])


class ClaudeCardTest(ReadingCase):
    def render(self, lang="ko", body=None, now=NOW):
        return mod.render_card(self.reading(body, now=now), mod.label_set(lang), now)

    def test_renders_exactly_the_card_the_poller_wrote(self):
        self.assertEqual(self.render(), LIVE_CARD)

    def test_renders_the_same_windows_in_english(self):
        card = self.render("en")

        self.assertEqual(
            [(row["title"], row["formattedValue"]) for row in card["metrics"]],
            [("Session", "47%"), ("reset", "2h 2m\n"),
             ("Weekly", "77%"), ("reset", "3d 10h\n"),
             ("Fable", "66%"), ("reset", "3d 10h")],
        )

    def test_the_language_moves_the_labels_and_nothing_else(self):
        korean, english = self.render("ko"), self.render("en")

        self.assertEqual(
            [row.get("normalizedValue") for row in korean["metrics"]],
            [row.get("normalizedValue") for row in english["metrics"]],
        )
        self.assertEqual(korean["metricsBarValue"], english["metricsBarValue"])

    def test_a_window_past_its_reset_shows_no_countdown(self):
        """We genuinely do not know the next Reset — Claude states no window
        length — so the countdown is omitted rather than extrapolated."""
        for lang, expected in (("ko", ["현재 세션"]), ("en", ["Session"])):
            with self.subTest(lang=lang):
                card = mod.render_card(
                    self.one_window(resets_at="2026-07-23T08:00:00Z"),
                    mod.label_set(lang), NOW,
                )
                self.assertEqual([row["title"] for row in card["metrics"]], expected)

    def test_windows_are_spaced_apart_and_the_last_one_is_not(self):
        metrics = self.render()["metrics"]

        self.assertTrue(metrics[1]["formattedValue"].endswith("\n"))
        self.assertTrue(metrics[3]["formattedValue"].endswith("\n"))
        self.assertFalse(metrics[5]["formattedValue"].endswith("\n"))

    def test_the_menu_bar_carries_the_session_and_weekly_windows(self):
        self.assertEqual(self.render()["metricsBarValue"], "47% · 77%")

    def test_the_clock_that_is_handed_in_is_the_one_that_is_used(self):
        later = NOW + 3600

        card = self.render(now=later)

        self.assertEqual(card["lastUpdatedDate"], "2026-07-23T09:17:25Z")
        self.assertEqual(card["metrics"][1]["formattedValue"], "1시간 2분 후\n")

    def test_a_reading_with_no_windows_renders_nothing_to_publish(self):
        """Nothing to render means the last-good Card stands, not an empty one."""
        self.assertIsNone(mod.render_card(self.reading({"limits": []}), mod.label_set("ko"), NOW))

    def test_a_window_that_renders_nothing_leaves_no_stray_spacing(self):
        """The spacer is smuggled into the previous row's value, so a Window that
        renders no rows would still hang a blank line off the Card's last row."""
        reading = mod.UsageReading(
            provider="claude", plan="Max 20x", captured_at=NOW,
            windows=(mod.Window(id=mod.SESSION_WINDOW, used=47.0, resets_at=SESSION_RESET),
                     mod.Window(id=mod.WEEKLY_WINDOW, used=None)),
        )

        card = mod.render_card(reading, mod.label_set("ko"), NOW)

        self.assertEqual([row["title"] for row in card["metrics"]], ["현재 세션", "재설정"])
        self.assertFalse(card["metrics"][-1]["formattedValue"].endswith("\n"))


class InterfaceLanguageTest(ReadingCase):
    def test_loading_the_module_never_shells_out(self):
        """`detect_lang()` runs `defaults`. At import that made every consumer pay
        for a subprocess and left the language impossible to vary in a test; it now
        happens when a Card is rendered."""
        probe = (
            "import subprocess, sys\n"
            "def refuse(*args, **kwargs):\n"
            "    raise AssertionError('the module shelled out while being imported')\n"
            "subprocess.run = refuse\n"
            f"sys.path.append({str(REPO)!r})\n"
            "import runcat_poll\n"
        )

        result = subprocess.run([sys.executable, "-c", probe],
                                capture_output=True, text=True, timeout=60)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_language_is_resolved_when_a_label_set_is_asked_for(self):
        self.patch("detect_lang", lambda: "ko")
        self.assertEqual(mod.label_set()["session"], "현재 세션")

        self.patch("detect_lang", lambda: "en")
        mod.interface_lang.cache_clear()
        self.assertEqual(mod.label_set()["session"], "Session")

    def test_the_language_is_determined_once_however_many_cards_are_rendered(self):
        """One run renders a Card per Provider, and determining the language runs
        `defaults`. The import-time global this replaced at least paid once."""
        detections = []
        self.patch("detect_lang", lambda: detections.append(1) or "ko")

        mod.label_set()
        mod.label_set()

        self.assertEqual(len(detections), 1)

    def test_an_explicit_language_needs_no_detection(self):
        self.patch("detect_lang", lambda: self.fail("detected a language it was given"))

        self.assertEqual(mod.label_set("en")["session"], "Session")
        self.assertEqual(mod.label_set("ko")["session"], "현재 세션")

    def test_every_language_carries_the_same_display_strings(self):
        self.assertEqual(set(mod.STRINGS["ko"]), set(mod.STRINGS["en"]))


if __name__ == "__main__":
    unittest.main()
