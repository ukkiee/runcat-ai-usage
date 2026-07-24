# runcat-ai-usage

**English** | [한국어](README.ko.md)

Show your **Claude Code** and **Codex** usage — session / weekly / per-model limits with reset countdowns — as a [RunCat Neo](https://github.com/runcat-dev/RunCatNeo) dashboard card and menu-bar meter.

```
Claude Code Max 20x            menu bar: 45% · 45%
  Session: 45%
  ▓▓▓▓▓░░░░░
  reset: 1h 53m

  Weekly: 45%
  ▓▓▓▓▓░░░░░
  reset: 4d 9h

  Fable: 45%
  ▓▓▓▓▓░░░░░
  reset: 4d 9h
```

Labels auto-switch between English and Korean based on your macOS language (see [Configuration](#configuration)).

## How it works

RunCat Neo can render any local JSON file as a custom-metrics card. This project ships a small Python poller that a `launchd` agent runs every 5 minutes via `runcat-poll.py`. Each run it:

1. Reuses the OAuth credentials **already on your machine** (no separate login):
   - **Claude** — read from the login Keychain (`Claude Code-credentials`) via Apple's signed `security` CLI, falling back to `~/.claude/.credentials.json` where Claude Code keeps the credential in a file instead (headless / remote logins).
   - **Codex** — read from `~/.codex/auth.json`.
2. Calls each provider's **dedicated usage endpoint** — a plain metadata `GET`, **not** a model request, so it costs **no tokens** and doesn't touch your rate limits:
   - Claude: `GET https://api.anthropic.com/api/oauth/usage`
   - Codex: `GET https://chatgpt.com/backend-api/wham/usage`
3. Writes `~/.claude/runcat-usage.json` and `~/.codex/runcat-usage.json`, which RunCat Neo watches and renders.

The numbers are the real, account-wide values (the same ones the official apps show), available even when the apps are closed.

> Credit: the usage endpoints and OAuth details were learned from [`openusage`](https://github.com/robinebers/openusage), a menu-bar app that does the same reverse-engineered lookups.

## Requirements

- macOS
- `python3` (standard library only — no `pip install`)
- [RunCat Neo](https://github.com/runcat-dev/RunCatNeo)
- Signed in to **Claude Code** (`~/.claude`) and/or **Codex** (`~/.codex`) — either or both

## Install

```sh
git clone https://github.com/ukkiee/runcat-ai-usage.git
cd runcat-ai-usage
./install.sh
```

Then in **RunCat Neo → Settings → Metrics → Custom Metrics → Add Custom Metrics Source**, add:

- `~/.claude/runcat-usage.json` (Claude Code)
- `~/.codex/runcat-usage.json` (Codex)

To show a value in the menu bar, click the Metrics Bar and toggle the source on.

`install.sh` writes a `launchd` agent that runs the poller from the cloned folder every 5 minutes (`RunAtLoad` fires it once immediately). Set a different cadence with `RUNCAT_POLL_INTERVAL=600 ./install.sh`.

### Uninstall

```sh
./uninstall.sh
```

Removes the `launchd` agent and the poller's own state files. The `runcat-usage.json` cards are left in place — remove the sources in RunCat Neo settings if you want.

## Configuration

Environment variables (set them in the `launchd` plist that `install.sh` writes, under a `EnvironmentVariables` dict, or export before a manual run):

| Variable | Default | Effect |
|---|---|---|
| `RUNCAT_LANG` | auto (macOS UI language) | `ko` or `en` to force the card language. |
| `RUNCAT_POLL_INTERVAL` | `300` | Poll interval in seconds (install-time only). |

Card labels and plan names live at the top of `runcat_poll.py` (the implementation beside the entry point):

- `STRINGS` — every display string per language: the `session` / `weekly` / `reset` row labels and the countdown wording. It holds no window identities, so translating a card changes labels only.
- `CODEX_PLAN_LABELS` — maps Codex `plan_type` to a display name (e.g. `prolite → "Pro 5x"`, `pro → "Pro 20x"`).
- Claude's plan (`Max 20x`, …) is derived automatically from its rate-limit tier.
- The per-model weekly cap uses the model's own name (e.g. `Fable`), so it follows whatever model your plan currently scopes.

## Auth & safety

- **Claude — read-only.** The access token is only *read* — from the Keychain, or from `~/.claude/.credentials.json` on a machine where Claude Code stores it there; **neither store is ever written**. An unsigned script can't do an ACL-preserving `SecItemUpdate` the way the signed apps do, and a coarse `security -U` write could lock Claude Code out of its own credential — so this tool refuses to refresh the Claude token. While the token is valid (i.e. you've used Claude recently) it polls live usage; once it expires it stops polling and instead rebuilds the card from the usage reading kept on the last successful poll (`~/.claude/runcat-reading.json`), zeroing any window whose reset time has passed since, until Claude Code refreshes its own token on your next use.
- **Codex — file-based.** If the `~/.codex/auth.json` token is near expiry it is refreshed via the standard OAuth endpoint and written back to that file (the same mechanism Codex uses). Codex tokens are long-lived, so this is rare.
- No credentials or tokens are ever printed, stored elsewhere, or sent anywhere except the provider's own usage endpoint.

## Caveats

- The usage endpoints are **undocumented/internal** (used by the official clients). They can change without notice, in which case this tool — and `openusage` — would need updating.
- Values reflect **this machine's** tokens. Usage from another device is picked up on the next successful poll here.
- Reset countdowns are text refreshed every poll (up to `RUNCAT_POLL_INTERVAL` stale), not a live-ticking timer. RunCat's menu bar is a single line, so the two menu-bar percentages are shown side by side (`45% · 45%`), not stacked.

## License

[MIT](LICENSE)

---

*Unofficial community integration. "RunCat" is a trademark of its respective authors; this project is not affiliated with or endorsed by them.*
