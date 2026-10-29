"""A model's Window can be left off every Card and brought back with one command.

`runcat-poll.py toggle Fable` flips the model in a small settings file and asks
the agent to poll at once. Only the Card changes: the Reading keeps the Window,
so showing it again needs nothing but a render.
"""

import io
import json
import plistlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import runcat_poll as mod  # the tests package puts the repo root on sys.path

NOW = 1784794645.0           # 2026-07-23T08:17:25Z
SESSION_RESET = 1784802000.0
WEEKLY_RESET = 1785092400.0


def reading():
    return mod.UsageReading(provider="claude", plan="Max 20x", captured_at=NOW, windows=(
        mod.Window(id=mod.SESSION_WINDOW, used=47.0, resets_at=SESSION_RESET),
        mod.Window(id=mod.WEEKLY_WINDOW, used=77.0, resets_at=WEEKLY_RESET),
        mod.Window(id=mod.scoped_window_id("Fable"), used=66.0, resets_at=WEEKLY_RESET,
                   label="Fable"),
    ))


def titles(card):
    return [row["title"].strip() for row in card["metrics"]]


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.patch("SETTINGS_FILE", self.dir / "settings.json")
        self.patch("log", lambda _msg: None)
        self.patch("detect_lang", lambda: "en")
        mod.interface_lang.cache_clear()
        self.addCleanup(mod.interface_lang.cache_clear)

    def patch(self, name, value):
        original = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(lambda: setattr(mod, name, original))

    def settings(self, data):
        mod.SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")


class RenderTest(Case):
    def test_a_hidden_model_is_left_off_the_card(self):
        card = mod.render_card(reading(), mod.label_set("en"), NOW, hidden=frozenset({"fable"}))

        self.assertNotIn("Fable", titles(card))
        self.assertEqual(titles(card)[:4], ["Plan", "Session", "reset", "Weekly"])

    def test_session_and_weekly_cannot_be_hidden(self):
        card = mod.render_card(reading(), mod.label_set("en"), NOW,
                               hidden=frozenset({"session", "weekly"}))

        self.assertIn("Session", titles(card))
        self.assertIn("Weekly", titles(card))

    def test_the_last_shown_window_still_closes_with_a_blank_line(self):
        card = mod.render_card(reading(), mod.label_set("en"), NOW, hidden=frozenset({"fable"}))

        self.assertTrue(card["metrics"][-1]["formattedValue"].endswith("\n"))


class SettingsTest(Case):
    def test_no_settings_file_hides_nothing(self):
        self.assertEqual(mod.hidden_models(), frozenset())

    def test_a_settings_file_we_cannot_use_hides_nothing(self):
        for data in ([], {"hiddenModels": "Fable"}, {"other": 1}):
            with self.subTest(data=data):
                self.settings(data)
                self.assertEqual(mod.hidden_models(), frozenset())

    def test_names_are_matched_without_case(self):
        self.settings({"hiddenModels": ["Fable", 3]})

        self.assertEqual(mod.hidden_models(), frozenset({"fable"}))


class ToggleTest(Case):
    def test_a_toggle_hides_and_the_next_one_shows_again(self):
        self.assertEqual(mod.toggle_model("Fable"), (True, ["Fable"]))
        self.assertEqual(mod.toggle_model("fable"), (False, []))

    def test_a_toggle_keeps_the_rest_of_the_settings(self):
        self.settings({"hiddenModels": ["Opus"], "other": 1})

        mod.toggle_model("Fable")

        self.assertEqual(json.loads(mod.SETTINGS_FILE.read_text(encoding="utf-8")),
                         {"hiddenModels": ["Opus", "Fable"], "other": 1})

    def test_the_card_hides_it_while_the_reading_keeps_it(self):
        card_path, reading_path = self.dir / "card.json", self.dir / "reading.json"
        mod.toggle_model("Fable")

        mod.publish("claude", "usage response", card_path, reading_path, reading(), NOW)

        card = json.loads(card_path.read_text(encoding="utf-8"))
        stored = json.loads(reading_path.read_text(encoding="utf-8"))
        self.assertNotIn("Fable", titles(card))
        self.assertIn(mod.scoped_window_id("Fable"), [w["id"] for w in stored["windows"]])


class CommandTest(Case):
    def run_main(self, *argv):
        polled = []

        def claude_homes(environ=None):
            if environ is None:  # only a poll reads the homes from its own environment
                polled.append("poll")
            return []

        self.patch("claude_homes", claude_homes)
        self.patch("codex_homes", lambda environ=None: [])
        self.patch("AGENT_PLIST", self.dir / "absent.plist")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = mod.main(list(argv))
        return code, out.getvalue(), polled

    def test_toggle_flips_the_setting_and_does_not_poll_itself(self):
        code, out, polled = self.run_main("toggle", "Fable")

        self.assertEqual((code, polled), (0, []))
        self.assertIn("Fable: hidden", out)
        self.assertEqual(mod.hidden_models(), frozenset({"fable"}))

    def test_toggling_twice_in_a_row_shows_both_times(self):
        """launchd drops a start request while the agent is still running, which is
        why the toggle redraws the Cards itself instead of asking for a poll."""
        home = mod.ClaudeHome(self.dir / ".claude")
        home.root.mkdir()
        mod.write_atomic(home.reading, mod.reading_as_json(reading()))
        self.patch("claude_homes", lambda environ=None: [home])
        self.patch("codex_homes", lambda environ=None: [])
        self.patch("AGENT_PLIST", self.dir / "absent.plist")

        for expected in (False, True):
            with redirect_stdout(io.StringIO()):
                mod.main(["toggle", "Fable"])
            card = json.loads(home.card.read_text(encoding="utf-8"))
            self.assertEqual("Fable" in titles(card), expected)

    def test_the_homes_come_from_the_installed_agent(self):
        plist = self.dir / "agent.plist"
        plist.write_bytes(plistlib.dumps({"EnvironmentVariables": {
            "RUNCAT_CLAUDE_HOMES": "~/.claude,~/.claude-sub"}}))
        self.patch("AGENT_PLIST", plist)

        self.assertEqual(mod.agent_environment(),
                         {"RUNCAT_CLAUDE_HOMES": "~/.claude,~/.claude-sub"})

    def test_toggle_without_a_model_changes_nothing(self):
        code, _out, polled = self.run_main("toggle")

        self.assertEqual((code, polled), (2, []))
        self.assertFalse(mod.SETTINGS_FILE.exists())

    def test_no_arguments_polls(self):
        _code, _out, polled = self.run_main()

        self.assertEqual(polled, ["poll"])


if __name__ == "__main__":
    unittest.main()
