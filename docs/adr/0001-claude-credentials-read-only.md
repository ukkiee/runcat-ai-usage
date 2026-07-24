# Claude credentials are read-only; we never write the Keychain

Codex tokens live in a plain file and can be refreshed and written back, so the obvious symmetry would be to refresh Claude's token the same way. We deliberately do not: the Claude token lives in the macOS login Keychain, and this poller is an unsigned script, so it can read the credential but cannot update it the way the signed Claude Code app does. Instead, when the Claude token is valid we poll live usage, and when it has expired we stop polling and fall back to a locally computed **Stale Reading** until Claude Code refreshes its own token on next use.

## Considered options

- **`SecItemUpdate` via ctypes** — the ACL-preserving call the signed apps use. Measured: an unsigned interpreter calling it raises a GUI Keychain authorization prompt, which blocks indefinitely under `launchd` where nobody can answer it.
- **`security add-generic-password -U`** — no prompt, because the `security` binary is Apple-signed, but the coarse update can reset the item's access control list and lock Claude Code out of its own credential. The failure mode is the user being logged out of Claude Code by a usage widget.
- **Refresh without writing back** — rejected outright. Anthropic rotates refresh tokens, so consuming one without persisting the replacement invalidates the copy Claude Code still holds. This causes exactly the logout we are trying to avoid.

Reading via the signed `security` CLI was measured to work with no prompt, so read-only access is both safe and sufficient.

## Where we read it from

The Keychain is Claude Code's credential home on a desktop login, but not on every machine: a headless or remote login writes `~/.claude/.credentials.json` instead, and there the Keychain holds no item at all. We read that file when the Keychain has nothing to give, which is the difference between a live card and one that never leaves its Stale Reading. The read-only rule covers it unchanged — Claude Code rotates that file too, so writing it back would be the same logout, only in a different place.

## Consequences

Claude usage is live only while its token is valid — roughly, while you have used Claude within the last hour. Beyond that the card shows a Stale Reading, and usage incurred on another machine is not reflected until this machine polls successfully again. That gap is the price of never touching the Keychain, and it is why the Stale Reading concept exists at all.
