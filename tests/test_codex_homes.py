"""Every Codex home is its own account, with its own Card.

`CODEX_HOME` lets one machine hold several Codex logins side by side. Each home
named in RUNCAT_CODEX_HOMES is polled with its own auth.json, and everything the
poller keeps for it lands beside that auth.json — never beside another home's,
or one account's Card would show the other's usage.
"""

import json
import tempfile
import unittest
from pathlib import Path

import runcat_poll as mod  # the tests package puts the repo root on sys.path

NOW = 1784806000.0        # 2026-07-23T11:26:40Z
HOUR = 3600.0
DAY = 86400.0


def response(weekly_used):
    return {"plan_type": "prolite", "rate_limit": {
        "primary_window": {"used_percent": weekly_used, "limit_window_seconds": 604800,
                           "reset_at": NOW + 5 * DAY},
        "secondary_window": {"used_percent": 40, "limit_window_seconds": 18000,
                             "reset_at": NOW + 2 * HOUR},
    }}


class CodexHomesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = Path(self.tmp.name).resolve()
        self.patch("CODEX_DEFAULT_HOME", self.user / ".codex")

    def patch(self, name, value):
        original = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(lambda: setattr(mod, name, original))

    def homes(self, value):
        return mod.codex_homes({mod.CODEX_HOMES_ENV: value} if value is not None else {})

    def test_without_the_setting_only_the_default_home_is_polled(self):
        for value in (None, "", " , "):
            with self.subTest(value=value):
                self.assertEqual(self.homes(value), [mod.CodexHome(self.user / ".codex")])

    def test_each_named_home_is_polled_in_order(self):
        homes = self.homes(f"{self.user}/.codex, {self.user}/.codex2")

        self.assertEqual([home.root for home in homes], [self.user / ".codex", self.user / ".codex2"])

    def test_the_default_home_keeps_no_name_and_the_others_are_named_after_their_directory(self):
        homes = self.homes(f"{self.user}/.codex,{self.user}/.codex2,{self.user}/work")

        self.assertEqual([home.name for home in homes], ["", "codex2", "work"])

    def test_a_home_named_twice_is_polled_once(self):
        """Two polls of one auth.json in a run could spend the same refresh token twice."""
        homes = self.homes(f"{self.user}/.codex2,{self.user}/.codex2/")

        self.assertEqual(len(homes), 1)

    def test_a_label_names_the_account_on_the_card(self):
        homes = self.homes(f"personal={self.user}/.codex,{self.user}/.codex2")

        self.assertEqual([home.account for home in homes], ["personal", "codex2"])
        self.assertEqual([home.tag for home in homes], ["codex (personal)", "codex (codex2)"])
        self.assertEqual([home.root for home in homes], [self.user / ".codex", self.user / ".codex2"])

    def test_an_entry_with_a_label_but_no_path_is_ignored(self):
        self.assertEqual(self.homes("personal="), [mod.CodexHome(self.user / ".codex")])

    def test_a_tilde_is_expanded_because_launchd_never_does(self):
        homes = mod.codex_homes({mod.CODEX_HOMES_ENV: "~/.codex2"})

        self.assertEqual(homes[0].root, (Path.home() / ".codex2").resolve())

    def test_everything_a_home_keeps_sits_beside_its_own_auth_json(self):
        home = mod.CodexHome(self.user / ".codex2", "codex2")

        self.assertEqual({path.parent for path in (home.auth_file, home.card, home.reading,
                                                   home.rotation_lost)},
                         {self.user / ".codex2"})


class TwoHomesPollTest(unittest.TestCase):
    """Two logins polled in one run, each answering with its own usage."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        user = Path(self.tmp.name)
        self.first = mod.CodexHome(user / ".codex")
        self.second = mod.CodexHome(user / ".codex2", "codex2")
        for home, token in ((self.first, "first-access"), (self.second, "second-access")):
            home.root.mkdir()
            home.auth_file.write_text(json.dumps({"tokens": {
                "access_token": token, "refresh_token": token + "-refresh", "account_id": token,
            }}), encoding="utf-8")

        self.logs = []
        self.patch("log", self.logs.append)
        self.patch("detect_lang", lambda: "ko")
        mod.interface_lang.cache_clear()
        self.addCleanup(mod.interface_lang.cache_clear)
        self.patch("jwt_exp", lambda _token: NOW + 30 * DAY)
        self.usage = {"Bearer first-access": response(13), "Bearer second-access": response(90)}
        self.reachable = True
        self.patch("http_get_json", self.fetch)

    def patch(self, name, value):
        original = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(lambda: setattr(mod, name, original))

    def fetch(self, url, headers, timeout=15):
        if not self.reachable:
            raise RuntimeError("the usage endpoint is unreachable")
        return 200, self.usage[headers["Authorization"]]

    def poll_both(self, now=NOW):
        for home in (self.first, self.second):
            mod.codex_poll(home, now)

    @staticmethod
    def card(home):
        return json.loads(home.card.read_text(encoding="utf-8"))

    def test_each_home_writes_its_own_accounts_usage_to_its_own_card(self):
        self.poll_both()

        self.assertEqual(self.card(self.first)["metrics"][1]["formattedValue"], "87% 남음")
        self.assertEqual(self.card(self.second)["metrics"][1]["formattedValue"], "10% 남음")

    def test_every_home_names_its_directory_in_the_first_row(self):
        self.poll_both()

        self.assertEqual(self.card(self.first)["metrics"][0],
                         {"title": "codex", "formattedValue": "Pro 5x\n"})
        self.assertEqual(self.card(self.second)["metrics"][0],
                         {"title": "codex2", "formattedValue": "Pro 5x\n"})

    def test_each_home_keeps_its_own_reading(self):
        self.poll_both()

        weekly = [json.loads(home.reading.read_text(encoding="utf-8"))["windows"][0]["used"]
                  for home in (self.first, self.second)]
        self.assertEqual(weekly, [13.0, 90.0])

    def test_a_recovered_card_keeps_the_name_of_its_home(self):
        self.poll_both()
        self.reachable = False

        self.poll_both(now=NOW + 3 * HOUR)

        self.assertEqual(self.card(self.second)["metrics"][0]["title"], "codex2")
        self.assertIn("codex (codex2): usage fetch failed", " ".join(self.logs))


if __name__ == "__main__":
    unittest.main()
