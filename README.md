# runcat-ai-usage

**English** | [한국어](README.ko.md)

Show how much of your **Claude Code** and **Codex** limits is left — session / weekly / per-model limits with reset countdowns — as a [RunCat Neo](https://github.com/runcat-dev/RunCatNeo) dashboard card and menu-bar meter.

```
Claude                         menu bar: 55% · 55%
  claude: Max 20x

  Session: 55% left
  ▓▓▓▓▓░░░░░
  reset: 1h 53m

  Weekly: 55% left
  ▓▓▓▓▓░░░░░
  reset: 4d 9h

  Fable: 55% left
  ▓▓▓▓▓░░░░░
  reset: 4d 9h

  Last updated: now
```

Labels auto-switch between English and Korean based on your macOS language (see [Configuration](#configuration)).

## How it works

RunCat Neo can render any local JSON file as a custom-metrics card. This project ships a small Python poller that a `launchd` agent runs every 5 minutes via `runcat-poll.py`. Each run it:

1. Reuses the OAuth credentials **already on your machine** (no separate login):
   - **Claude** — read from the login Keychain (`Claude Code-credentials`) via Apple's signed `security` CLI, falling back to `~/.claude/.credentials.json` where Claude Code keeps the credential in a file instead (headless / remote logins). Every Claude home listed in `RUNCAT_CLAUDE_HOMES` is read the same way (see [Several Claude accounts](#several-claude-accounts)).
   - **Codex** — read from `~/.codex/auth.json`, or from the `auth.json` of every Codex home listed in `RUNCAT_CODEX_HOMES` (see [Several Codex accounts](#several-codex-accounts)).
2. Calls each provider's **dedicated usage endpoint** — a plain metadata `GET`, **not** a model request, so it costs **no tokens** and doesn't touch your rate limits:
   - Claude: `GET https://api.anthropic.com/api/oauth/usage`
   - Codex: `GET https://chatgpt.com/backend-api/wham/usage`
3. Writes a `runcat-usage.json` in each Claude home (`~/.claude/runcat-usage.json` by default) and in each Codex home (`~/.codex/runcat-usage.json` by default), which RunCat Neo watches and renders.

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

### Several Codex accounts

If you keep more than one Codex login in separate homes (e.g. `CODEX_HOME=~/.codex2 codex`), list every home when installing:

```sh
RUNCAT_CODEX_HOMES="~/.codex,~/.codex2" ./install.sh
```

Each home gets its own card beside its own `auth.json`, so add `~/.codex2/runcat-usage.json` in RunCat Neo as well. A card from any home other than `~/.codex` names its directory in the card's first row (`codex2: Pro 5x`). Homes are never discovered on their own: a backup copy of `auth.json` would otherwise be refreshed and sign the real login out.

### Several Claude accounts

If you run a second Claude Code login with its own config directory (e.g. `CLAUDE_CONFIG_DIR=$HOME/.claude-sub claude`), list every home when installing:

```sh
RUNCAT_CLAUDE_HOMES="~/.claude,~/.claude-sub" ./install.sh
```

Add `~/.claude-sub/runcat-usage.json` in RunCat Neo as well; its card's first row reads `claude-sub: Max 20x`. Claude Code stores that login in a Keychain item named after a hash of the directory path (`Claude Code-credentials-<8 hex>`), so `CLAUDE_CONFIG_DIR` must be the plain absolute path: no trailing slash, and not reached through a symlink, or the poller looks for an item that does not exist. As with the default login, the card stays live only while that login's token is valid, which means using that account within roughly the last hour.

Every card's first row names its home after its directory (`claude: Max 20x`, `claude-sub: Max 20x`). To name a home yourself, write `label=path`, e.g. `RUNCAT_CLAUDE_HOMES="personal=~/.claude,~/.claude-sub"` gives `personal: Max 20x`. The label only changes what the card shows; the Keychain item is still found from the path.

When you reinstall, pass every variable you set before (`RUNCAT_CLAUDE_HOMES`, `RUNCAT_CODEX_HOMES`) again, since `install.sh` rewrites the agent from scratch.

### Uninstall

```sh
./uninstall.sh
```

Removes the `launchd` agent and the poller's own state files. The `runcat-usage.json` cards are left in place — remove the sources in RunCat Neo settings if you want.

## Configuration

Environment variables (export `RUNCAT_CLAUDE_HOMES` / `RUNCAT_CODEX_HOMES` / `RUNCAT_LANG` when running `install.sh` and it writes them into the `launchd` plist, or export them before a manual run):

| Variable | Default | Effect |
|---|---|---|
| `RUNCAT_LANG` | auto (macOS UI language) | `ko` or `en` to force the card language. |
| `RUNCAT_CLAUDE_HOMES` | `~/.claude` | Comma-separated Claude Code config directories to poll, one card each. Write `label=path` to choose the name a card shows (`personal=~/.claude`). |
| `RUNCAT_CODEX_HOMES` | `~/.codex` | Comma-separated Codex homes to poll, one card each. Write `label=path` to choose the name a card shows (`personal=~/.codex`). |
| `RUNCAT_POLL_INTERVAL` | `300` | Poll interval in seconds (install-time only). |

Card labels and plan names live at the top of `runcat_poll.py` (the implementation beside the entry point):

- `STRINGS` — every display string per language: the `session` / `weekly` / `reset` row labels, the `left` share wording and the countdown wording. It holds no window identities, so translating a card changes labels only.
- `CODEX_PLAN_LABELS` — maps Codex `plan_type` to a display name (e.g. `prolite → "Pro 5x"`, `pro → "Pro 20x"`).
- Claude's plan (`Max 20x`, …) is derived automatically from its rate-limit tier.
- The per-model weekly cap uses the model's own name (e.g. `Fable`), so it follows whatever model your plan currently scopes.

## Auth & safety

- **Claude — read-only.** The access token is only *read* — from the Keychain, or from `~/.claude/.credentials.json` on a machine where Claude Code stores it there; **neither store is ever written**. An unsigned script can't do an ACL-preserving `SecItemUpdate` the way the signed apps do, and a coarse `security -U` write could lock Claude Code out of its own credential — so this tool refuses to refresh the Claude token. While the token is valid (i.e. you've used Claude recently) it polls live usage; once it expires it stops polling and instead rebuilds the card from the usage reading kept on the last successful poll (`~/.claude/runcat-reading.json`), showing any window whose reset time has passed since as fully left, until Claude Code refreshes its own token on your next use.
- **Codex — file-based.** If the token in a Codex home's `auth.json` is near expiry it is refreshed via the standard OAuth endpoint and written back to that file (the same mechanism Codex uses). Codex tokens are long-lived, so this is rare.
- No credentials or tokens are ever printed, stored elsewhere, or sent anywhere except the provider's own usage endpoint.

## Caveats

- The usage endpoints are **undocumented/internal** (used by the official clients). They can change without notice, in which case this tool — and `openusage` — would need updating.
- Values reflect **this machine's** tokens. Usage from another device is picked up on the next successful poll here.
- Reset countdowns are text refreshed every poll (up to `RUNCAT_POLL_INTERVAL` stale), not a live-ticking timer. RunCat's menu bar is a single line, so the two menu-bar percentages are shown side by side (`55% · 55%`), not stacked.

## License

[MIT](LICENSE)

---

*Unofficial community integration. "RunCat" is a trademark of its respective authors; this project is not affiliated with or endorsed by them.*
