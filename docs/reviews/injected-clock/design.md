# Design — Injected clock

Slug: `injected-clock`
Status: settled, pre-implementation
Target: `runcat_poll.py` (the four import-time `NOW_*` globals and their ten readers), and every test that calls a poll or `main` directly — `tests/test_stale_reading.py`, `tests/test_codex_reading.py`, `tests/test_codex_credentials.py`, `tests/test_entry_point.py`

## Problem

The clock is half-threaded. The **pure** layer already takes the moment a poll runs as a parameter — `decay(reading, now)`, `render_card(reading, labels, now)`, `plausible_reset(value, now, id)`, `claude_reading(body, plan, now)`, `codex_reading(body, plan, now)`. But the **impure** layer reads a moment frozen at import and feeds it in:

```python
NOW = datetime.now(timezone.utc)   # runcat_poll.py:62 — computed once, at import
NOW_EPOCH = NOW.timestamp()        # :63
NOW_MS = NOW_EPOCH * 1000          # :64
NOW_ISO = NOW.strftime(ISO_SECONDS)# :65
```

So "now" enters the system from two directions. Ten reads across seven impure functions draw from the frozen globals:

| read | site | what it needs |
|---|---|---|
| `render_card(decay(stale, NOW_EPOCH), …, NOW_EPOCH)` | `recover_card` :566 | epoch (twice) |
| `carry_forward_resets(…, NOW_EPOCH)` | `publish` :581 | epoch |
| `render_card(reading, …, NOW_EPOCH)` | `publish` :582 | epoch |
| `expires_at > NOW_MS` | `claude_poll` :730 | epoch ms |
| `claude_reading(body, …, NOW_EPOCH)` | `claude_poll` :747 | epoch |
| `exp - NOW_EPOCH <= REFRESH_BUFFER_S` | `codex_poll` :938 | epoch |
| `codex_reading(body, …, NOW_EPOCH)` | `codex_poll` :985 | epoch |
| `print(f"[{NOW_ISO}] {msg}")` | `log` :387 | iso string |
| `cur["last_refresh"] = NOW.strftime(…)` | `codex_persist_rotation` :877 | datetime |
| `"at": NOW_ISO` | `codex_record_rotation_loss` :891 | iso string |

In production this freeze is harmless: each launchd tick is a fresh `python3` process, so `datetime.now()` at import is captured seconds before use, and `fmt_duration` floors to whole minutes while the token checks sit inside a five-minute buffer. A ~30–45 s intra-tick skew can at most read a countdown one minute high.

The cost is entirely in the tests, and it is real. The two poll-level suites each grow an `at()` helper whose only job is to reach into module state:

```python
def at(self, moment):                       # test_stale_reading.py:73, test_codex_reading.py:98
    self.patch("NOW_EPOCH", moment)
    self.patch("NOW_MS", moment * 1000)
    self.patch("NOW_ISO", mod.epoch_to_iso(moment))
    # codex additionally: self.patch("jwt_exp", lambda _t: moment + 30*DAY)
```

- `at()` is duplicated across both files.
- It fires ~55 `setattr`+cleanup registrations across the two suites just to move a clock (≈27 + ≈28).
- The clock and the token-expiry stub are **entangled**: `codex_poll` reads `NOW_EPOCH` at :938 to decide whether to refresh, so `at()` must re-patch `jwt_exp` in lock-step or time-travel spuriously trips the refresh branch.
- Fixtures read the global back out to build their own input: `expires_at = (mod.NOW_MS - 1) if token_expired else (mod.NOW_MS + 60_000)` (test_stale_reading.py:89).

This is on the settled `usage-reading` design's *Out of scope* list (`design.md:126`, "the remaining import-time globals"), deferred deliberately until the arc landed. It has now landed.

## The deepening

`main()` already loops over the two polls — the one place a poll begins:

```python
for name, fn in (("claude", claude_poll), ("codex", codex_poll)):
    try:
        fn()
```

Compute the moment there, once, and thread it through the impure layer to meet the pure layer that already accepts it. "Now" stops being module state and becomes a value that enters at one seam.

```
main() ──now──▶ claude_poll(now) / codex_poll(now)
                      │
                      ├──▶ publish(…, now) ──▶ render_card(reading, labels, now)
                      └──▶ recover_card(…, now) ──▶ render_card(decay(stale, now), labels, now)
```

The deletion test: pulling the clock out of module state concentrates "when is now?" at the one place a poll begins and drains ~55 global-mutations of ceremony out of the tests. The pure layer is already shaped to receive it; only the impure layer and the tests change.

## Decisions

### 1. One clock per tick, born in `main()`

`main()` computes `now = datetime.now(timezone.utc).timestamp()` once and passes the same value to `claude_poll(now)` and `codex_poll(now)`. The two Providers render against one shared moment.

The alternative — each poll computing its own `now` at its start — would give Codex a fresher clock after Claude's HTTP round-trip, but it relocates the impurity into each function rather than removing it, and the tests would then have to patch that internal call. The determinism a single per-tick clock buys is the whole point; the freshness it costs is bounded by minute-flooring and a five-minute buffer, so it is invisible.

### 2. Thread a single epoch float

`now` is one epoch-seconds float — exactly what the pure layer already accepts (`decay`, `render_card`, `plausible_reset`, `claude_reading`, `codex_reading` all take `now: float`). The one site needing milliseconds converts inline (`expires_at > now * 1000`, :730); `lastUpdatedDate` is already derived from the epoch by `epoch_to_iso(now)` inside `finalize`.

A richer `Clock`/`Moment` object carrying `.epoch`/`.ms`/`.iso` would be Speculative Generality — an adapter over a seam where exactly one thing varies (the scalar second). Only build it if a second representation ever genuinely diverges.

### 3. The three residual reads take wall-clock, not the poll's `now`

`log` (:387), `codex_persist_rotation`'s `last_refresh` (:877) and `codex_record_rotation_loss`'s `at` (:891) each want *the real moment the side effect happened*, not the poll's logical `now`. A log line emitted 40 s into a poll should say 40 s later; `last_refresh` and `at` answer "when did this write happen". These read wall-clock directly, via a small helper (e.g. `stamp()` returning a timezone-aware `datetime`, formatted per site — `ISO_SECONDS` for `log` and the rotation-loss marker, the `.000Z` millisecond form for `last_refresh`).

Threading `now` into `log` would pollute its ~21 call sites for what is a logging concern. Test determinism is unaffected: tests replace `log` wholesale with a list append (its timestamp is never asserted), and no test asserts on `last_refresh` or the rotation-loss `at`.

### 4. `now` is a required parameter; the four globals are deleted

`claude_poll`, `codex_poll`, `publish`, `recover_card`, and the two `*_recover` wrappers all take `now` as a required parameter. `NOW`, `NOW_EPOCH`, `NOW_MS`, `NOW_ISO` are deleted outright. `ISO_SECONDS` and `REFRESH_BUFFER_S` are constants and stay.

A defaulted parameter (`now=None` computing wall-clock when absent) was rejected: it reintroduces the impure clock source inside each function — the exact thing being removed, merely relocated. `main()` is the only *production* caller and passes `now`. There are, however, four *test* callers of the polls or `main`, and a required parameter is exactly what forces each to declare the moment it runs at rather than inheriting a hidden one — every one of them is migrated in decision 5. A required parameter states the dependency at every seam — accept dependencies, don't create them.

### 5. Full test migration — every direct caller of a poll or `main`

"Full" means all four suites that reach the impure layer, not just the two poll-level ones. A repository-wide search for `claude_poll`/`codex_poll`/`main(` calls and for `NOW_*` reads is the inventory this decision rests on:

- **`test_stale_reading.py`, `test_codex_reading.py`** — the poll-level suites. Pass `now=` into `self.poll(...)`; delete both `at()` helpers and all `NOW_*` patching; compute the `jwt_exp` stub from the test's own `now` rather than keeping it in sync with a patched global.
- **`test_codex_credentials.py`** — calls `mod.codex_poll()` directly at seven sites and derives its expiry stub as `jwt_exp = lambda _t: mod.NOW_EPOCH + 1` (line 141), reading a global this change deletes. Each call passes an explicit reference `now`, and the stub derives from that same value. Without this the credential-rotation suite loses its clock fixture and every `codex_poll()` call becomes a `TypeError` against the now-required parameter.
- **`test_entry_point.py`** — replaces both poll functions with zero-argument lambdas (`lambda n=name: polled.append(n)`) and then runs `main` under `__main__`. Since `main()` now calls `fn(now)`, the stubs become `lambda now, n=name: polled.append(n)`, and the test can additionally assert `main` passed a numeric `now` — turning a would-be `TypeError` into positive coverage that the clock reaches the seam.

The pure-function suite (`test_usage_reading.py`) already passes `now` and does not change.

The `NOW = …` constant at the top of a test file stays — that is the test's own chosen reference moment, a plain value it passes as `now=NOW`, not module state anyone patches. What disappears is the act of patching a module global. A half-migration that kept `at()` and `NOW_*` patching would leave the friction this change exists to remove; the clean poll tests are the proof the seam got deeper — the interface is the test surface.

### 6. Clock only; the two Provider dups are separate tickets

This pass threads the clock and nothing else. Two small cleanups the Provider-seam exploration surfaced — folding the two byte-identical HTTP `except` blocks into one helper, and reconciling the two encodings of "nothing to publish" (Claude's `reading is None` skip vs Codex's empty-windows → `render_card` → `None`) — are filed as separate follow-up tickets.

The HTTP-`except` dedup overlaps the same `*_poll` functions, but bundling would blur the design gate and the diff so a regression could not be cleanly attributed. The "nothing to publish" reconciliation is subtler than a free-rider: Claude's `None` path deliberately skips even the publish/carry-forward (ticket 06's "unreconcilable scale → keep last-good, do not recover"), so merging it with Codex's empty-windows path touches settled semantics and deserves its own thought.

## Out of scope

- The Provider seam with two adapters — the exploration found a full adapter relocates complexity into an ADR-asymmetric `prepare_token` rather than concentrating it; the design's earlier deferral stands.
- Consolidating the renderer into its own module — presentation knowledge is already concentrated behind `render_card` (`runcat_poll.py:440–552`); a file split isn't earned at 500 lines.
- Making the Card/Reading output paths injectable — unchanged from the `usage-reading` design; none of the threaded functions needs it.

## Risks

- **A future caller that forgets `now`.** A required parameter makes this a `TypeError` at the call, not a silent wrong time — the failure is loud and immediate, which is the point of decision 4.
- **A genuinely long-running single tick.** Threading one per-tick clock makes the whole tick render against its start moment, so a tick that somehow ran for minutes would render a slightly staler countdown than per-poll clocks would. Bounded by minute-flooring; accepted in decision 1, and no observed tick runs longer than the two sequential HTTP timeouts.
- **The residual three drift from the poll's `now`.** Deliberate (decision 3): they record real side-effect time. The risk is only that someone later reads `last_refresh` expecting the poll's logical now; the field name ("last_refresh") already says otherwise.
