# Design — Usage Reading module

Slug: `usage-reading`
Status: settled, pre-implementation
Target: `runcat-poll.py` (496 lines, single module, no tests)

## Problem

There is no representation of a rate-limit **Window** anywhere in the codebase. The rendered RunCat **Card** JSON is the only form the data ever takes, so two things that should consume data instead consume presentation:

1. **Persistence.** `runcat-reset-state.json` is keyed by the *rendered row title* — `"Session"`, `"현재 세션"`, `"Fable"` (`runcat-poll.py:320`, `:473`, read at `:236`).
2. **Failure recovery.** `apply_reset_fallback` (`:224–249`) reverse-engineers the Card layout to make a data decision: it knows rows come in pairs, that the sub-row is identified by *absence* of `normalizedValue` (`:245`), and that the literals `"0%"` and `"—"` are meaningful.

This shallowness is already producing defects:

- **Changing language kills the fallback.** The sidecar holds `{"Session": …}` while the new Card's rows are titled `"현재 세션"`. Every `resets.get()` misses and the fallback silently becomes a no-op. It self-heals only on a *successful* poll — but the fallback exists precisely for when polls are failing, so the two failure modes are correlated.
- **A model rename does the same thing**, with no user action needed, because `scope.model.display_name` is the key and `scope.model.id` is `null` in the live response.
- **The fallback permanently destroys the Card's blank-line spacing.** The spacer is smuggled into a value string as a trailing `\n` (`:197`); the fallback overwrites that value with `"—"` (`:246`), and the `!= "—"` guard at `:245` means it never gets re-spaced until a live poll rebuilds everything.
- **Reset epochs are trusted with no plausibility check** (`:236–243`). Codex's `reset_at` is taken raw from an undocumented endpoint (`:468`) with nothing asserting it is seconds-not-milliseconds or absolute-not-relative, while Claude's goes through `iso_to_epoch` (`:180`). A single bad value renders every Window at 0% while the user is actually near their limit.

## The deepening

Introduce a **Usage Reading** — a provider-neutral record of every Window for one Provider at one moment. It becomes the persisted artifact; the Card becomes a pure derivative.

```
Provider response ──map──▶ Usage Reading ──render──▶ Card JSON
                                │
                                ├──persist──▶ runcat-reading.json
                                │
                          (poll failed)
                                │
                             decay ──▶ Usage Reading ──render──▶ Card JSON
```

Three pure functions fall out, and they are the test surface:

- **map** — provider response → Usage Reading
- **render** — Usage Reading + clock + labels → Card JSON
- **decay** — Usage Reading + clock → Usage Reading with reset Windows zeroed

## Decisions

### 1. Window identity is a closed vocabulary, never a display label

Identity is one of `session`, `weekly`, `weekly_scoped:<model>`. The display label is a separate field and is never used for matching.

Mapping in:
- Claude `limits[]` — `kind: session → session`, `weekly_all → weekly`, `weekly_scoped → weekly_scoped:<scope.model.display_name>`
- Codex — `limit_window_seconds: 18000 → session`, `604800 → weekly`

Why the model name is part of the identity despite being vendor-controlled: `scope.model.id` is `null` in the live response, so the display name is the only handle available. Dropping it makes identity fully rename-proof but causes two simultaneous model-scoped Windows to collide into one key — which silently merges two different limits. A rename costs one poll cycle without fallback for that Window; a collision produces wrong data indefinitely. Collision is the worse failure, so the qualifier stays.

### 2. Persist the whole Usage Reading, not just reset times

`runcat-reading.json` holds every Window (id, label, used share, reset moment) plus the moment it was captured. Recovery becomes: load Reading → decay → render. The Card is never patched in place.

This is what dissolves the spacing defect — the Card is rebuilt from data rather than edited, so no code outside `render` needs to know that spacing is encoded in a value string.

Persisting is a **merge on reset moments**, not a blind replace: a Window whose new reset fails validation keeps whatever reset is already persisted under the same identity (decision 5). Used shares always come from the new response.

### 3. Clock and labels arrive as arguments; paths stay as they are

`render` and `decay` take a clock and a label set. `detect_lang()` moves from import time to call time, which removes the `defaults` subprocess from module import.

Because identity is now id-based (decision 1), the mapping layer needs no labels at all — `WINDOW_LABELS` becomes an id map and every display string moves into `render`'s label set. The two sources of label truth collapse into one.

Output paths remain module constants. Making them injectable is a separate, larger change and none of the three pure functions needs them.

### 4. Split the entry point from the importable module

`runcat-poll.py` stays exactly where it is, permanently, as the executable entry point. The implementation moves beside it into `runcat_poll.py`, and the entry point does nothing but run that module.

The hyphen is the reason the split exists: it makes the file unimportable by name, so every test today has to go through `importlib.util.spec_from_file_location`. Separating the two roles gives tests a plain import without ever moving the path that installed launchd plists hold as an absolute string.

There is deliberately **no removal date and no plist migration**. An installed plist keeps naming `runcat-poll.py` for the life of the project, so there is no release boundary at which anyone breaks, and nothing ever has to rewrite or reload launchd configuration on a user's behalf. `install.sh` also keeps pointing at `runcat-poll.py`, so existing and new installs converge on one path rather than diverging.

Single file for the implementation is retained. A package split is more structure than 500 lines earns.

### 5. Validate at the Reading boundary

The Reading is now both the domain representation and the persisted, trusted artifact, so bad values must not get in:

- Used share outside 0–100 is clamped, and the clamp is logged.
- A reset moment outside `now − 1 day … now + 30 days` is discarded; that Window carries no reset and therefore gets no countdown and no decay entry.

The range catches both plausible unit errors: milliseconds land tens of thousands of years out, relative-seconds land in 1970.

Two limits on what this achieves, both deliberate:

- **Range checking is not schema validation.** A clamp bounds a value; it cannot tell that a number is on the wrong scale. A legacy `utilization` of `0.75` meaning 75% passes a 0–100 check untouched and would render as 0.75%. Scale is therefore established per field from observed responses (decision 8), never inferred from range.
- **A failed reset never destroys a good one.** If a Window's new reset fails validation but the same identity already carries a valid persisted reset, the persisted one is kept. Only a Window with no usable reset from either source ends up with no countdown and no decay entry. Blind replacement would let a single transient schema glitch inside an otherwise successful response erase the only state a later outage could have decayed from.

### 6. A Window past its reset shows no countdown

When a Reading is stale and a Window's reset has passed, its used share reads zero and the countdown row is omitted entirely. We genuinely do not know the next reset: Codex supplies `limit_window_seconds` and could be extrapolated, but Claude's `limits[]` carries no window duration, so extrapolating there would require assuming 5h/7d by convention. Omitting is uniform across Providers and adds no assumption.

### 7. New state file, no migration

`runcat-reading.json` is new. The old `runcat-reset-state.json` has schema `{label: epoch}` and is distinguishable by the absence of a `windows` key; an unrecognised file is treated as absent. Worst case is a single poll cycle with no Stale Reading available, after which the next successful poll writes the new file. `uninstall.sh` gains the obsolete file in its list.

### 8. The legacy Claude response shape is kept and tested

Claude currently returns both `limits[]` and the top-level `five_hour`/`seven_day`, but the legacy path (`:342`) never executes while `limits[]` is present, so it is unverified and assumes without checking that `utilization` shares the 0–100 scale of `percent`.

Now that both shapes produce the same Usage Reading, keeping the legacy path costs one small mapper rather than a parallel pipeline. It stays, and it gets a test — it is the only safety net if the undocumented `limits[]` shape disappears.

The scale question is settled by evidence, not by clamping. A live response observed while designing this carried `five_hour.utilization = 18.0` alongside `limits[].percent = 18` for the same Window, which fixes `utilization` on the same 0–100 scale as `percent`. That observation is what the legacy mapper stands on, and it is the first thing to re-check if the legacy path ever starts producing surprising numbers. Should a future response contradict it — values that cannot be reconciled with a 0–100 reading — the legacy mapper publishes nothing and the previous Reading stands: silently underreporting usage at the very moment the primary shape disappears is worse than showing a stale Card.

### 9. Tests use the standard library

`unittest`, run with `python3 -m unittest`. The README promises "standard library only — no pip install"; adding pytest would contradict it and raise the contribution barrier.

Coverage that must land with this change:

- **map** — Claude `limits[]`, Claude legacy, Codex
- **render** — Korean and English, Window with and without a countdown
- **decay** — Window past reset zeroed and its countdown dropped; Window not yet reset untouched; repeated decay is idempotent
- **boundary validation** — out-of-range used share clamped; millisecond, relative-seconds, and non-numeric reset moments all discarded
- **reset carry-forward** — a malformed reset on a Window that already has a persisted reset keeps the persisted one, and an outage after that reset still decays the Window

## Out of scope

Deliberately not in this change:

- The Provider seam with two adapters (the duplicated poll pipeline).
- Making output paths injectable, and the remaining import-time globals.
- Consolidating the remaining RunCat presentation knowledge into one renderer — most of it follows from decision 2 anyway.
- **The Codex token rotation defect** (`:403–414`): `codex_refresh` may consume a rotating refresh token and `codex_write_back` can then drop it with a bare `return`, logging out the Codex CLI. Independent of this design and more urgent; it should be fixed on its own.

## Risks

- **Model rename still costs a cycle.** Accepted in decision 1; the alternative is worse.
- **A 30-day reset ceiling is a guess.** No observed Window is wider than 7 days, so the ceiling has four times the headroom, but a Provider introducing a monthly Window would have its reset discarded. The clamp is logged, so it would surface rather than fail silently.
- **Two files carry the poller, permanently.** The entry point and the implementation stay separate for the life of the project. That is a small standing cost, and what it buys is that no installed plist ever has to change and no migration code ever has to exist — deployment stops being a source of silent staleness rather than merely deferring it.
