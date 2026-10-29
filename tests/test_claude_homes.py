"""Every Claude home is its own account, with its own Card.

`CLAUDE_CONFIG_DIR` lets one machine hold several Claude Code logins side by
side. Each home named in RUNCAT_CLAUDE_HOMES is polled with its own Keychain
item, and its Card and Reading land inside that directory, never beside another
home's, or one account's Card would show the other's usage.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import runcat_poll as mod  # the tests package puts the repo root on sys.path

NOW = 1784806000.0        # 2026-07-23T11:26:40Z
HOUR = 3600.0


def response(weekly_used):
    return {"limits": [
        {"kind": "session", "percent": 40, "resets_at": "2026-07-23T13:26:40Z"},
        {"kind": "weekly_all", "percent": weekly_used, "resets_at": "2026-07-28T11:26:40Z"},
    ]}


class Patching(unittest.TestCase):
    def patch(self, name, value):
        original = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(lambda: setattr(mod, name, original))


class ClaudeHomesTest(Patching):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = Path(self.tmp.name).resolve()
        self.patch("CLAUDE_DEFAULT_HOME", self.user / ".claude")

    def homes(self, value):
        return mod.claude_homes({mod.CLAUDE_HOMES_ENV: value} if value is not None else {})

    def test_without_the_setting_only_the_default_home_is_polled(self):
        for value in (None, "", " , "):
            with self.subTest(value=value):
                self.assertEqual(self.homes(value), [mod.ClaudeHome(self.user / ".claude")])

    def test_the_default_home_keeps_no_name_and_the_others_are_named_after_their_directory(self):
        homes = self.homes(f"{self.user}/.claude, {self.user}/.claude-sub,{self.user}/work")

        self.assertEqual([home.root for home in homes],
                         [self.user / ".claude", self.user / ".claude-sub", self.user / "work"])
        self.assertEqual([home.name for home in homes], ["", "claude-sub", "work"])

    def test_a_home_named_twice_is_polled_once(self):
        homes = self.homes(f"{self.user}/.claude-sub,{self.user}/.claude-sub/")

        self.assertEqual(len(homes), 1)

    def test_a_tilde_is_expanded_because_launchd_never_does(self):
        homes = mod.claude_homes({mod.CLAUDE_HOMES_ENV: "~/.claude-sub"})

        self.assertEqual(homes[0].root, Path.home() / ".claude-sub")

    def test_a_symlinked_home_keeps_the_path_it_was_named_by(self):
        """The Keychain item is hashed from the path Claude Code was started with,
        so resolving the link would look for an item that does not exist."""
        (self.user / "real").mkdir()
        os.symlink(self.user / "real", self.user / ".claude-sub")

        self.assertEqual(self.homes(f"{self.user}/.claude-sub")[0].root, self.user / ".claude-sub")

    def test_everything_a_home_keeps_sits_inside_it(self):
        home = mod.ClaudeHome(self.user / ".claude-sub", "claude-sub")

        self.assertEqual({path.parent for path in (home.creds_file, home.card, home.reading)},
                         {self.user / ".claude-sub"})


class KeychainServiceTest(unittest.TestCase):
    def test_the_default_home_uses_the_bare_item_name(self):
        self.assertEqual(mod.ClaudeHome(Path("/Users/someone/.claude")).keychain_service,
                         "Claude Code-credentials")

    def test_another_home_uses_the_item_claude_code_names_after_its_path(self):
        """Pinned to the item Claude Code created for
        CLAUDE_CONFIG_DIR=/Users/ukyi/.claude-sub on a real machine."""
        home = mod.ClaudeHome(Path("/Users/ukyi/.claude-sub"), "claude-sub")

        self.assertEqual(home.keychain_service, "Claude Code-credentials-77e4c220")

    def test_the_keychain_is_asked_for_the_homes_own_item(self):
        asked = []

        class Result:
            returncode = 44
            stdout = ""

        class FakeSubprocess:
            TimeoutExpired = subprocess.TimeoutExpired

            @staticmethod
            def run(args, **_kwargs):
                asked.append(args[args.index("-s") + 1])
                return Result()

        original = mod.subprocess
        mod.subprocess = FakeSubprocess
        self.addCleanup(setattr, mod, "subprocess", original)

        with tempfile.TemporaryDirectory() as tmp:
            home = mod.ClaudeHome(Path(tmp), "sub")
            mod.claude_read_token(home)

        self.assertEqual(asked, [home.keychain_service])


class TwoHomesPollTest(Patching):
    """Two logins polled in one run, each answering with its own usage."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        user = Path(self.tmp.name)
        self.first = mod.ClaudeHome(user / ".claude")
        self.second = mod.ClaudeHome(user / ".claude-sub", "claude-sub")
        for home in (self.first, self.second):
            home.root.mkdir()

        self.logs = []
        self.patch("log", self.logs.append)
        self.patch("detect_lang", lambda: "ko")
        mod.interface_lang.cache_clear()
        self.addCleanup(mod.interface_lang.cache_clear)
        tokens = {self.first: "first-access", self.second: "second-access"}
        self.patch("claude_read_token", lambda home: {
            "accessToken": tokens[home], "expiresAt": (NOW + 24 * HOUR) * 1000,
            "rateLimitTier": "default_claude_max_20x",
        })
        self.usage = {"Bearer first-access": response(13), "Bearer second-access": response(90)}
        self.reachable = True
        self.patch("http_get_json", self.fetch)

    def fetch(self, url, headers, timeout=15):
        if not self.reachable:
            raise RuntimeError("the usage endpoint is unreachable")
        return 200, self.usage[headers["Authorization"]]

    def poll_both(self, now=NOW):
        for home in (self.first, self.second):
            mod.claude_poll(home, now)

    @staticmethod
    def card(home):
        return json.loads(home.card.read_text(encoding="utf-8"))

    def test_each_home_writes_its_own_accounts_usage_to_its_own_card(self):
        self.poll_both()

        weekly = [self.card(home)["metrics"][2]["formattedValue"] for home in (self.first, self.second)]
        self.assertEqual(weekly, ["87% 남음", "10% 남음"])

    def test_only_a_home_other_than_the_default_names_itself_in_the_title(self):
        self.poll_both()

        self.assertEqual(self.card(self.first)["title"], "Claude Code Max 20x")
        self.assertEqual(self.card(self.second)["title"], "Claude Code Max 20x (claude-sub)")

    def test_a_recovered_card_keeps_the_name_of_its_home(self):
        self.poll_both()
        self.reachable = False

        self.poll_both(now=NOW + HOUR)

        self.assertEqual(self.card(self.second)["title"], "Claude Code Max 20x (claude-sub)")
        self.assertIn("claude (claude-sub): usage fetch failed", " ".join(self.logs))


if __name__ == "__main__":
    unittest.main()
