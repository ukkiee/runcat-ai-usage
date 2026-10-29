"""RunCat Neo usage poller — Claude Code + Codex.

Importable implementation. This module is deliberately NOT executable: the single
entry point is `runcat-poll.py` beside it, whose path installed launchd agents
own. Leaving a second runnable path here would let installs diverge, which is
exactly what keeping one fixed entry point prevents.

Reads the OAuth credentials already on this machine and calls each provider's
dedicated usage endpoint (a plain metadata GET — no model inference, no token
cost) to write real, account-wide rate-limit numbers into the RunCat Neo Cards.
Designed to run on a launchd interval (every ~5 min).

Card shape:
- title is the Provider's name ("Claude", "Codex"). The first row names the
  account after its directory, and its plan: "claude: Max 20x",
  "claude-sub: Max 20x".
- each rate-limit window is two rows: the share still left (with the bar) and,
  underneath it, a bar-less "reset in <time>" line so the countdown isn't cramped
  next to the percentage. Windows are "Session" (5h) and "Weekly" (7d).

Every Codex home named in RUNCAT_CODEX_HOMES (comma-separated, default ~/.codex)
is its own account and gets its own Card beside its own auth.json. Claude works
the same way with RUNCAT_CLAUDE_HOMES (default ~/.claude): each entry is a
CLAUDE_CONFIG_DIR, and its Card sits in that directory.

Claude's response becomes a Usage Reading first — every Window with a stable
identity, a bounded used share and a Reset we can believe — and the Card is
rendered from that Reading with the clock and the label set handed in. What the
user sees is downstream of data rather than being the only place it exists.

Design (safe / read-mostly):
- Codex tokens live in each home's auth.json (a plain file). We may refresh an
  expired token and write it back to that file — the same mechanism Codex uses.
- Claude tokens live in the macOS Keychain. We READ the access token via the
  Apple-signed `security` CLI (no GUI prompt) but NEVER write the keychain: an
  unsigned script can't do an ACL-preserving SecItemUpdate the way the signed
  apps do, and a `security -U` write could lock Claude Code out of its own
  credential. So when the Claude token is valid we poll live usage; when it is
  expired we do NOT refresh — instead we rebuild the Card from the Usage Reading
  kept on the last successful poll, zeroing any Window whose Reset has passed
  since. Claude Code refreshes its own token whenever you use it, and the next
  poll goes live again.

A provider that errors leaves its existing Card untouched (last-good) when there
is nothing to rebuild from.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

ISO_SECONDS = "%Y-%m-%dT%H:%M:%SZ"

HOME = Path.home()
REFRESH_BUFFER_S = 5 * 60  # refresh (Codex) / treat-expired margin


# ------------------------- interface language -------------------------

def detect_lang():
    """'ko' or 'en'. RUNCAT_LANG overrides; otherwise the macOS UI language."""
    override = os.environ.get("RUNCAT_LANG", "").strip().lower()
    if override.startswith("ko"):
        return "ko"
    if override.startswith("en"):
        return "en"
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            code = line.strip().strip('(),"').lower()
            if code:
                return "ko" if code.startswith("ko") else "en"
    except Exception:
        pass
    for var in ("LANG", "LC_ALL", "LC_MESSAGES"):
        if os.environ.get(var, "").lower().startswith("ko"):
            return "ko"
    return "en"


# Every display string a Card can carry, per language. Window identities are
# never in here — they are a closed vocabulary of their own, so translating a
# Card moves labels only.
STRINGS = {
    "ko": {
        "session": "현재 세션",
        "weekly": "주간 한도",
        "reset": "재설정",
        "plan": "플랜",
        "account": "계정",
        "left": "{share} 남음",
        "days": "{days}일 {hours}시간",
        "hours": "{hours}시간 {minutes}분",
        "minutes": "{minutes}분",
        "moment": "1분 미만",
        "countdown": "{duration} 후",
    },
    "en": {
        "session": "Session",
        "weekly": "Weekly",
        "reset": "reset",
        "plan": "Plan",
        "account": "Account",
        "left": "{share} left",
        "days": "{days}d {hours}h",
        "hours": "{hours}h {minutes}m",
        "minutes": "{minutes}m",
        "moment": "<1m",
        "countdown": "{duration}",
    },
}


@functools.lru_cache(maxsize=1)
def interface_lang():
    """The language this run renders in.

    Cached because it cannot change inside a single run and one run renders a
    Card per Provider: detect_lang() shells out to `defaults`, and asking it once
    per Card would spend a subprocess on an answer we already have.
    """
    return detect_lang()


def label_set(lang=None):
    """The display strings for one interface language.

    The language is resolved when a Card is rendered, never at import: a module
    that shells out on the way in makes every consumer pay for it and leaves the
    language impossible to vary.
    """
    return STRINGS.get(lang or interface_lang(), STRINGS["en"])


# ---------------------------- Usage Reading ----------------------------
#
# A provider-neutral record of every Window for one Provider at one moment.
# Everything the user sees is derived from it, so it is also where bad values
# have to be stopped.

SESSION_WINDOW = "session"
WEEKLY_WINDOW = "weekly"
SCOPED_WINDOW_PREFIX = "weekly_scoped:"
LENGTH_WINDOW_PREFIX = "window_length:"   # + seconds, for a Window we have no name for

# How far from now a Reset may sit and still be believable. The range catches
# both plausible unit errors: milliseconds land tens of thousands of years out,
# and a relative offset mistaken for a moment lands in 1970.
RESET_PAST_S = 86400            # a day behind, so a just-passed Reset survives
RESET_FUTURE_S = 30 * 86400     # four times the widest Window anyone publishes


def scoped_window_id(model):
    """The identity of a Model-Scoped Window.

    The model's name is part of the identity even though the Provider controls
    it: `scope.model.id` is null in the live response, so the display name is the
    only handle there is. A rename costs one poll cycle without a fallback for
    that Window; dropping the qualifier would collide two simultaneous
    Model-Scoped Windows into one identity and merge two different limits for
    good. Collision is the worse failure.
    """
    return SCOPED_WINDOW_PREFIX + model


@dataclass(frozen=True)
class Window:
    """One rate-limit Window of a Usage Reading.

    `id` is what everything matches on and is never a display string, so changing
    the interface language moves labels only. `label` carries the name for a
    Window no label set can have an entry for — a Model-Scoped Window's model, or
    the length of a Window a Provider has introduced since — and is empty for the
    Session and Weekly Windows every Provider has. `resets_at` is an epoch second
    or None, and None means no countdown and nothing to decay from.
    """

    id: str
    used: float
    resets_at: float | None = None
    label: str = ""


@dataclass(frozen=True)
class UsageReading:
    """Every Window of one Provider at one moment, plus what it takes to title the
    Card. Frozen because a Reading is a record: recovery re-renders it rather than
    patching what was already published."""

    provider: str
    plan: str
    windows: tuple[Window, ...]
    captured_at: float


def finite_number(value):
    """`value` as a float when it really is a number, else None.

    Guards bool (an int in Python) and the NaN / Infinity that `json` accepts but
    that no share or moment can be.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def used_share(value, window_id):
    """A Window's used share as a percentage bounded to 0–100, or None when the
    Provider sent something that is not a number.

    An out-of-range value is clamped *and* logged. Clamping silently would hide a
    Provider changing scale under us, and the clamp itself cannot catch that: it
    bounds a value, it cannot tell that a number is on the wrong scale.
    """
    share = finite_number(value)
    if share is None:
        return None
    bounded = min(100.0, max(0.0, share))
    if bounded != share:
        log(f"{window_id}: used share {share:g} is outside 0–100 — clamped to {bounded:g}")
    return bounded


def plausible_reset(value, now, window_id):
    """A Reset we can believe, in epoch seconds, or None.

    The Reading is the trusted artifact, so a moment that cannot be one is dropped
    rather than persisted: a single bad value would otherwise render every Window
    at 0% while the user is actually near their limit.
    """
    moment = finite_number(value)
    if moment is None:
        if value is not None:
            log(f"{window_id}: reset {value!r} is not a number — no countdown")
        return None
    if not (now - RESET_PAST_S <= moment <= now + RESET_FUTURE_S):
        log(f"{window_id}: reset {moment:.0f} is not a plausible moment — discarded")
        return None
    return moment


def carry_forward_resets(reading, stored, now):
    """A Window whose new Reset did not survive validation keeps the one already
    persisted under the same identity. Used shares always come from the new
    response — only Resets are merged.

    Blind replacement would let one transient glitch inside an otherwise good
    response erase the only state a later outage could have decayed from. Only a
    Reset still ahead of `now` is carried: one that has already passed says
    nothing about the next one, and carrying it would decay a share we have only
    just measured.
    """
    if stored is None:
        return reading
    kept = {window.id: window.resets_at for window in stored.windows
            if window.resets_at is not None and window.resets_at > now}
    windows = tuple(window if window.resets_at is not None
                    else replace(window, resets_at=kept.get(window.id))
                    for window in reading.windows)
    return replace(reading, windows=windows)


def decay(reading, now):
    """The Reading as it stands at `now`, for when we could not replace it.

    A Window whose Reset has passed *since the Reading was captured* is back to
    zero and carries no Reset at all — we genuinely do not know the next one,
    because Claude states no window length to extrapolate from. A Window whose
    Reset had already passed when the Reading was captured is left alone: its used
    share was measured after that Reset, so zeroing it would underreport usage,
    which is the one failure worse than showing a stale Card.

    Decaying an already-decayed Reading changes nothing.
    """
    windows = tuple(
        replace(window, used=0.0, resets_at=None)
        if window.resets_at is not None and reading.captured_at < window.resets_at <= now
        else window
        for window in reading.windows
    )
    return replace(reading, windows=windows)


def reading_as_json(reading):
    """The Reading as it goes to disk. Every Window is written under its identity
    and never under its label, so a Reading captured in one interface language is
    still readable in another."""
    return {
        "provider": reading.provider,
        "plan": reading.plan,
        "capturedAt": reading.captured_at,
        "windows": [{"id": window.id, "label": window.label,
                     "used": window.used, "resetsAt": window.resets_at}
                    for window in reading.windows],
    }


def reading_from_json(data):
    """A Reading read back from disk, or None when what is there is not one.

    The obsolete reset-state file is `{label: epoch}` with no `windows` key, so it
    reads as absent rather than as a Reading with nothing in it. The worst case is
    a single cycle with no Stale Reading, after which the next successful poll
    writes the file this reads.

    A persisted Reset is deliberately NOT put back through plausible_reset: it was
    checked when it was written, and an outage long enough to push it more than a
    day into the past is exactly the case decay exists for. Re-checking it here
    would throw away the state recovery runs on.
    """
    if not isinstance(data, dict):
        return None
    # Typed before it is looked up: an unhashable provider would raise from the
    # `in`, and a parser that raises is not "treated as absent".
    provider = data.get("provider")
    rows = data.get("windows")
    captured_at = finite_number(data.get("capturedAt"))
    if not isinstance(provider, str) or provider not in CARD_HEADINGS:
        return None
    if not isinstance(rows, list) or captured_at is None:
        return None

    windows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        window_id = row.get("id")
        if not isinstance(window_id, str) or not window_id:
            continue
        share = used_share(row.get("used"), window_id)
        if share is None:
            continue
        label = row.get("label")
        windows.append(Window(id=window_id, used=share,
                              resets_at=finite_number(row.get("resetsAt")),
                              label=label if isinstance(label, str) else ""))
    if not windows:
        return None

    plan = data.get("plan")
    return UsageReading(provider=provider, plan=plan if isinstance(plan, str) else "",
                        windows=tuple(windows), captured_at=captured_at)


# ---- Claude constants (from Claude Code / OpenUsage) ----
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_USAGE_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "anthropic-beta": "oauth-2025-04-20",
    "User-Agent": "claude-code/2.1.69",
}
CLAUDE_DEFAULT_HOME = HOME / ".claude"
CLAUDE_HOMES_ENV = "RUNCAT_CLAUDE_HOMES"

# ---- Codex constants ----
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_REFRESH_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_DEFAULT_HOME = HOME / ".codex"
CODEX_HOMES_ENV = "RUNCAT_CODEX_HOMES"

CODEX_PLAN_LABELS = {
    "free": "Free", "plus": "Plus", "pro": "Pro 20x", "prolite": "Pro 5x",
    "team": "Team", "business": "Business", "enterprise": "Enterprise",
}
# window length (seconds) -> Window identity
WINDOW_IDS = {18000: SESSION_WINDOW, 604800: WEEKLY_WINDOW}


# ----------------------------- helpers -----------------------------

def stamp():
    """The real wall-clock moment, timezone-aware.

    A poll's logical `now` is threaded in from `main()` so a Card, its Reading and
    its countdowns are all derived from one instant. This is the other clock: the
    moment a side effect actually happens. A log line emitted 40 s into a poll
    should read 40 s later than the poll began, and `last_refresh` / the
    rotation-loss `at` answer "when did this write happen" — none of them is the
    poll's logical moment, so they read the wall clock here instead of taking `now`.
    """
    return datetime.now(timezone.utc)


def log(msg):
    print(f"[{stamp().strftime(ISO_SECONDS)}] {msg}", file=sys.stderr)


def load_json(path):
    """Whatever is at `path`, or None when it is not JSON we can read.

    ValueError rather than JSONDecodeError: bytes that are not UTF-8 raise
    UnicodeDecodeError, and every caller here is reading a file it must treat as
    absent when it cannot be understood — one that raises instead would take the
    whole poll down with it, on every run, until someone deleted the file.
    """
    try:
        with Path(path).open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".runcat-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def http_get_json(url, headers, timeout=15):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r)


def iso_to_epoch(s):
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def epoch_to_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(ISO_SECONDS)


# ------------------------------- Card -------------------------------

CARD_HEADINGS = {
    "claude": {"name": "Claude", "symbol": "staroflife"},
    "codex": {"name": "Codex", "symbol": "camera.aperture"},
}


def fmt_duration(seconds, labels):
    """Compact 'time left' in the Card's language: 4일 15시간 / 4d 15h etc."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return labels["days"].format(days=days, hours=hours)
    if hours:
        return labels["hours"].format(hours=hours, minutes=minutes)
    if minutes:
        return labels["minutes"].format(minutes=minutes)
    return labels["moment"]


def window_title(window, labels):
    """What the Card calls a Window: the label set names the ones every Provider
    has, and a Model-Scoped Window carries the model's own name instead."""
    return labels.get(window.id) or window.label or window.id


def left_share(used):
    """The share of a Window still left, 0–100, from the used share the Reading
    records. Rounded so a float subtraction never reaches the Card as 87.30000001."""
    return round(100.0 - max(0.0, min(float(used), 100.0)), 4)


def window_rows(title, used, resets_at_epoch, labels, now):
    """A window's share-left row (with bar) plus a bar-less 'reset in <time>' row.

    The Reading keeps the used share; only the Card turns it around, so a Stale
    Reading written before the Card showed what is left still renders correctly.

    A Reset that has already passed gets no countdown at all: we genuinely do not
    know the next one, because Claude states no window length to extrapolate from.
    """
    if not isinstance(used, (int, float)):
        return []
    left = left_share(used)
    rows = [{"title": title, "formattedValue": labels["left"].format(share=f"{left:g}%"),
             "normalizedValue": round(left / 100, 4)}]
    if isinstance(resets_at_epoch, (int, float)) and resets_at_epoch - now > 0:
        dur = fmt_duration(resets_at_epoch - now, labels)
        rows.append({"title": labels["reset"],
                     "formattedValue": labels["countdown"].format(duration=dur)})
    return rows


def join_blocks(blocks):
    """Concatenate per-window row groups with a blank line after each one, so the
    blocks stand apart from each other and the last one stands apart from the
    'last updated' line RunCat Neo draws under the rows.

    RunCat has no spacer row, so the blank line is a newline smuggled into a
    row's text. It normally trails the block's last value. RunCat draws a row's
    bar under its text, though, so on a row with a bar that newline would open
    a gap between the text and its own bar; such a block hands the blank line
    to the next block, as a newline in front of its first title. The last
    block has no next block, so a bar there sits right on the footer. A group
    with no rows takes no spacer."""
    metrics = []
    owed = False
    for rows in blocks:
        if not rows:
            continue
        rows = [dict(row) for row in rows]
        if owed:
            rows[0]["title"] = "\n" + rows[0]["title"]
        owed = "normalizedValue" in rows[-1]
        if not owed:
            rows[-1]["formattedValue"] += "\n"
        metrics.extend(rows)
    return metrics


def bar_two(*used):
    """Menu-bar text from up to two used shares, shown as what is left, single line
    (RunCat's menu-bar view is single-line; a newline just gets truncated).
    e.g. used 18 and 39 -> '82% · 61%'."""
    parts = [f"{left_share(u):g}%" for u in used if isinstance(u, (int, float))]
    return " · ".join(parts) if parts else None


def account_rows(plan, account, labels):
    """The row under the title that says which login this is and on what Plan,
    named after its directory: 'claude: Max 20x', 'claude-sub: Max 20x'.

    It is a row rather than a second title line because rows are drawn in
    RunCat's smaller caption font, so it reads as a subtitle. RunCat offers no
    way to set any text grey."""
    if account:
        return [{"title": account, "formattedValue": plan}] if plan else \
               [{"title": labels["account"], "formattedValue": account}]
    return [{"title": labels["plan"], "formattedValue": plan}] if plan else []


def finalize(title, symbol, metrics, bar_value, now):
    """Assemble a Card. bar_value = menu-bar text; None falls back to the single
    window with the least left."""
    rows = [m for m in metrics if m is not None]
    if bar_value is None:
        bar = min((m["normalizedValue"] for m in rows if "normalizedValue" in m), default=None)
        bar_value = f"{bar * 100:g}%" if bar is not None else None
    card = {"title": title, "symbol": symbol, "metrics": rows, "lastUpdatedDate": epoch_to_iso(now)}
    if bar_value is not None:
        card["metricsBarValue"] = bar_value
    return card


def render_card(reading, labels, now, account=""):
    """A Usage Reading as the Card RunCat Neo reads.

    The clock and the label set are handed in, so the same Reading renders the
    same Card whatever the interface language is and whenever it is asked for —
    which is what lets a recovered Reading be re-rendered instead of a published
    Card being patched in place. Returns None when there is nothing to publish,
    leaving the last-good Card standing.

    `account` names which of a Provider's logins the Reading belongs to. It comes
    from where the Reading was read, never from the Reading, so it is handed in
    the same way the clock is.
    """
    windows = [window_rows(window_title(window, labels), window.used, window.resets_at, labels, now)
               for window in reading.windows]
    if not any(windows):
        return None
    metrics = join_blocks([account_rows(reading.plan, account, labels), *windows])
    shares = {window.id: window.used for window in reading.windows}
    return finalize(
        CARD_HEADINGS[reading.provider]["name"],
        CARD_HEADINGS[reading.provider]["symbol"],
        metrics,
        bar_two(shares.get(SESSION_WINDOW), shares.get(WEEKLY_WINDOW)),
        now,
    )


def log_tag(provider, account=""):
    """How log lines name one login: 'codex', or 'codex (codex2)'."""
    return f"{provider} ({account})" if account else provider


def recover_card(provider, why, card_path, reading_path, now, account=""):
    """Rebuild a Provider's Card from its Stale Reading, for a poll that could not
    happen.

    The Card is rebuilt from the record rather than patched where it stands. That
    is what keeps the blank line between Windows: the spacer is a trailing newline
    smuggled into the previous row's value, and patching the published Card
    overwrote it for good. It is also why a language change no longer breaks
    recovery — Windows are matched by identity, never by the row title on screen.
    """
    stale = reading_from_json(load_json(reading_path))
    card = render_card(decay(stale, now), label_set(), now, account) if stale else None
    tag = log_tag(provider, account)
    if card is None:
        log(f"{tag}: {why} — no Stale Reading to rebuild from, keeping last-good")
        return
    write_atomic(card_path, card)
    log(f"{tag}: {why} — Card rebuilt from the Stale Reading")


def publish(provider, why, card_path, reading_path, reading, now, account=""):
    """Publish a Provider's Card, or keep the last-good Card when the response gave
    nothing to publish.

    "Nothing to publish" reaches here two ways that end at the same outcome, and
    this is the one place both are reconciled. `reading` is None when the mapping
    could not reconcile the response at all — an unreconcilable legacy scale, which
    the mapper has already logged the reason for — and a Reading with no renderable
    Windows when the response simply carried none this poll. Either way the
    last-good Card stands and the Stale Reading is left untouched: this keeps
    last-good, it does not recover (decay) from the Stale Reading. That non-recovery
    is the distinction the legacy-scale refusal rests on — underreporting usage the
    moment the safety net is first used would be worse than a stale Card.

    The Reset a Window arrives without is taken from the one already persisted
    under the same identity, so one transient glitch inside an otherwise good
    response cannot erase the only state a later outage could decay from.
    """
    if reading is None:
        return  # unreconcilable response; the mapper has already logged why
    reading = carry_forward_resets(reading, reading_from_json(load_json(reading_path)), now)
    card = render_card(reading, label_set(), now, account)
    tag = log_tag(provider, account)
    if card is None:
        log(f"{tag}: {why} had no windows — keeping last-good")
        return
    write_atomic(card_path, card)
    write_atomic(reading_path, reading_as_json(reading))
    log(f"{tag}: live usage written")


def fetch_usage(url, headers, recover, now):
    """Fetch a Provider's usage over HTTP, recovering the Card from its Stale
    Reading on any failure.

    The fetch-or-recover shape is identical for both Providers — an HTTP status
    error recovers with "usage HTTP <code>", anything else with "usage fetch
    failed (<e>)" — so it is stated once here. What genuinely differs between the
    Providers (credential acquisition, header assembly, Codex's refresh/rotation)
    stays in each poll; this covers only the shared shape.

    Returns (True, body) on success, and (False, None) when the fetch failed and
    recovery has already run — the caller returns without publishing. The flag is
    what a bare `body` could not carry: a response that is legitimately JSON null
    is (True, None), a distinct case from a fetch that never landed.
    """
    try:
        _status, body = http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        recover(f"usage HTTP {e.code}", now)
        return False, None
    except Exception as e:
        recover(f"usage fetch failed ({e})", now)
        return False, None
    return True, body


# ----------------------------- Claude -----------------------------

def claude_plan_label(oauth):
    """'default_claude_max_20x' -> 'Max 20x'; fallback to subscriptionType."""
    tier = (oauth.get("rateLimitTier") or "").strip()
    for prefix in ("default_claude_", "claude_", "default_"):
        if tier.startswith(prefix):
            tier = tier[len(prefix):]
            break
    if tier:
        parts = [p if re.fullmatch(r"\d+x", p) else p.capitalize() for p in tier.split("_")]
        return " ".join(parts)
    sub = (oauth.get("subscriptionType") or "").strip()
    return sub.capitalize() if sub else ""


def home_entries(raw):
    """(label, path) for each comma-separated entry of a *_HOMES setting. An
    entry is a path, or 'label=path' to choose the name the Card shows."""
    entries = []
    for entry in raw.split(","):
        label, sep, path = entry.partition("=")
        label, path = (label.strip(), path.strip()) if sep else ("", entry.strip())
        if path:
            entries.append((label, path))
    return entries


@dataclass(frozen=True)
class ClaudeHome:
    """One Claude Code login on this machine: a CLAUDE_CONFIG_DIR.

    Each home is a separate account, so its Card and Reading sit inside that
    directory and never beside another home's. `name` is empty for ~/.claude
    and is what tells the Keychain item apart; any other home is named after
    its directory ('~/.claude-sub' -> 'claude-sub'). `label` is a name the
    user gave it in RUNCAT_CLAUDE_HOMES ('personal=~/.claude'), and wins
    wherever the account is shown.
    """

    root: Path
    name: str = ""
    label: str = ""

    @property
    def account(self):
        """What the Card and the log call this login: its label, else its
        directory name ('~/.claude' -> 'claude')."""
        return self.label or self.name or self.root.name.lstrip(".") or str(self.root)

    @property
    def keychain_service(self):
        """The Keychain item Claude Code keeps this login's credential under.

        ~/.claude uses the bare name. A login started with CLAUDE_CONFIG_DIR adds
        the first 8 hex digits of the SHA-256 of that path, so the hash only
        matches when CLAUDE_CONFIG_DIR is spelled as this absolute path (no `~`,
        no trailing slash, no detour through a symlink).
        """
        if not self.name:
            return CLAUDE_KEYCHAIN_SERVICE
        path = unicodedata.normalize("NFC", str(self.root))
        return f"{CLAUDE_KEYCHAIN_SERVICE}-{hashlib.sha256(path.encode('utf-8')).hexdigest()[:8]}"

    @property
    def creds_file(self):
        return self.root / ".credentials.json"

    @property
    def card(self):
        return self.root / "runcat-usage.json"

    @property
    def reading(self):
        return self.root / "runcat-reading.json"

    @property
    def tag(self):
        return log_tag("claude", self.account)


def claude_homes(environ=None):
    """The Claude homes to poll: RUNCAT_CLAUDE_HOMES (comma-separated, each a path
    or 'label=path') when set, else ~/.claude alone.

    Homes are named rather than discovered, the same as Codex homes: only a
    directory Claude Code was actually started with has a Keychain item to read.

    Paths are made absolute but not resolved through symlinks, because the
    Keychain item name is hashed from the path as written. The same directory
    named twice is still polled once.
    """
    entries = home_entries((os.environ if environ is None else environ).get(CLAUDE_HOMES_ENV, ""))
    if not entries:
        return [ClaudeHome(CLAUDE_DEFAULT_HOME)]

    default = CLAUDE_DEFAULT_HOME.expanduser().resolve()
    homes, seen = [], set()
    for label, entry in entries:
        root = Path(os.path.abspath(os.path.expanduser(entry)))
        if root.resolve() in seen:
            continue
        seen.add(root.resolve())
        is_default = root.resolve() == default
        homes.append(ClaudeHome(default if is_default else root,
                                "" if is_default else root.name.lstrip(".") or str(root),
                                label))
    return homes


def claude_read_token(home):
    """Read the Claude OAuth blob from the login Keychain via the signed `security`
    CLI (no GUI prompt). Times out rather than hanging launchd if a prompt ever
    appears. Falls back to the file store Claude Code keeps when the Keychain is
    not its credential home (headless / remote logins write
    `<home>/.credentials.json` instead). Both are read-only here.
    Returns the parsed claudeAiOauth dict or None."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", home.keychain_service, "-w"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        log(f"{home.tag}: keychain read timed out (prompt?) — skipping live poll")
        return None
    if out.returncode != 0:
        return claude_read_token_file(home)
    try:
        return (json.loads(out.stdout) or {}).get("claudeAiOauth") or claude_read_token_file(home)
    except json.JSONDecodeError:
        return claude_read_token_file(home)


def claude_read_token_file(home):
    """The same OAuth blob as the Keychain carries, from Claude Code's file store."""
    try:
        blob = json.loads(home.creds_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return (blob or {}).get("claudeAiOauth") or None


# Claude states a Window's kind; these are the two every account has.
CLAUDE_WINDOW_IDS = {"session": SESSION_WINDOW, "weekly_all": WEEKLY_WINDOW}


def claude_reset(value, now, window_id):
    """Claude states a Reset as an ISO-8601 moment."""
    moment = iso_to_epoch(value)
    if moment is None and value is not None:
        log(f"{window_id}: reset {value!r} is not a moment we can read — no countdown")
    return plausible_reset(moment, now, window_id)


def claude_window(window_id, label, percent, resets_at, now):
    """One Window of a Claude Reading, or None when its used share is unusable —
    a Window we cannot state a number for is worse than one that is absent."""
    share = used_share(percent, window_id)
    if share is None:
        return None
    return Window(id=window_id, used=share, label=label,
                  resets_at=claude_reset(resets_at, now, window_id))


def claude_limit_windows(limits, now):
    """`limits[]` uniformly carries the Session, Weekly and per-model
    Model-Scoped Windows. A kind we don't know is left out rather than guessed
    at: an identity invented here would not survive the next release."""
    windows = []
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        label = ""
        if limit.get("kind") == "weekly_scoped":
            model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
            label = model if isinstance(model, str) else ""
            window_id = scoped_window_id(label) if label else None
        else:
            window_id = CLAUDE_WINDOW_IDS.get(limit.get("kind"))
        if not window_id:
            continue
        window = claude_window(window_id, label, limit.get("percent"),
                               limit.get("resets_at"), now)
        if window is not None:
            windows.append(window)
    return windows


def claude_legacy_windows(body, now):
    """The older top-level Windows, for a response that no longer carries
    `limits[]`. Returns None when the response cannot be reconciled with the scale
    this mapping stands on.

    `utilization` is read on the same 0–100 scale as `percent`, on one piece of
    evidence: a live response observed while this was designed carried
    `five_hour.utilization = 18.0` beside `limits[].percent = 18` for the same
    Window. That observation is the whole basis, and it is the first thing to
    re-check if this path ever starts producing surprising numbers.

    Range checking cannot establish that scale, only contradict it. A fraction of
    `0.75` meaning 75% sits inside 0–100 untouched and would render as 0.75% — no
    clamp can tell that a number is on the wrong scale. What a value outside
    0–100 does tell us is that the evidence no longer holds, and clamping it into
    range would state a number we have no reason to believe. So nothing is
    published and the previous Reading stands: silently underreporting usage at
    the very moment this safety net is first used is worse than a stale Card.
    """
    windows = []
    for field, window_id in (("five_hour", SESSION_WINDOW), ("seven_day", WEEKLY_WINDOW)):
        section = body.get(field)
        if not isinstance(section, dict):
            continue
        share = finite_number(section.get("utilization"))
        if share is None:
            continue
        if not 0.0 <= share <= 100.0:
            log(f"claude: legacy {field}.utilization is {share:g}, which cannot be a "
                "percentage — the older fields are no longer on the scale this mapping "
                "reads them as, so nothing is published and the last Card stands")
            return None
        windows.append(Window(id=window_id, used=share,
                              resets_at=claude_reset(section.get("resets_at"), now, window_id)))
    return windows


def claude_reading(body, plan, now):
    """Claude's usage response as a Usage Reading, or None when the response
    cannot be trusted enough to publish anything from."""
    limits = body.get("limits")
    windows = (claude_limit_windows(limits, now) if isinstance(limits, list) and limits
               else claude_legacy_windows(body, now))
    if windows is None:
        return None
    return UsageReading(provider="claude", plan=plan,
                        windows=tuple(windows), captured_at=now)


def claude_recover(home, why, now):
    return recover_card("claude", why, home.card, home.reading, now, home.account)


def claude_poll(home, now):
    oauth = claude_read_token(home)
    token = (oauth or {}).get("accessToken") or ""
    expires_at = (oauth or {}).get("expiresAt")  # epoch ms
    token_ok = bool(token) and (not isinstance(expires_at, (int, float)) or expires_at > now * 1000)

    if not token_ok:
        # Expired / unreadable: never refresh (keychain-write unsafe). Recover.
        claude_recover(home, "token expired/absent", now)
        return

    headers = dict(CLAUDE_USAGE_HEADERS, Authorization=f"Bearer {token.strip()}")
    ok, body = fetch_usage(CLAUDE_USAGE_URL, headers, functools.partial(claude_recover, home), now)
    if not ok:
        return

    publish("claude", "usage response", home.card, home.reading,
            claude_reading(body, claude_plan_label(oauth), now), now, home.account)


# ----------------------------- Codex -----------------------------

@dataclass(frozen=True)
class CodexHome:
    """One Codex login on this machine: a CODEX_HOME directory and its auth.json.

    Each home is a separate account, so everything the poller keeps for it — the
    Card, the Reading, the rotation-loss marker — lives beside that auth.json and
    never beside another home's. `name` is empty for ~/.codex; any other home is
    named after its directory ('~/.codex2' -> 'codex2'). `label` is the name
    the user gave it in RUNCAT_CODEX_HOMES ('personal=~/.codex'), and wins
    wherever the account is shown.
    """

    root: Path
    name: str = ""
    label: str = ""

    @property
    def account(self):
        """What the Card and the log call this login: its label, else its
        directory name ('~/.claude' -> 'claude')."""
        return self.label or self.name or self.root.name.lstrip(".") or str(self.root)

    @property
    def auth_file(self):
        return self.root / "auth.json"

    @property
    def card(self):
        return self.root / "runcat-usage.json"

    @property
    def reading(self):
        return self.root / "runcat-reading.json"

    @property
    def rotation_lost(self):
        return self.root / "runcat-rotation-lost.json"

    @property
    def tag(self):
        return log_tag("codex", self.account)


def codex_homes(environ=None):
    """The Codex homes to poll: RUNCAT_CODEX_HOMES (comma-separated, each a path
    or 'label=path') when set, else ~/.codex alone.

    Homes are named explicitly rather than discovered. A backup such as
    ~/.codex.bak can hold a copy of a live credential, and refreshing that copy
    would rotate the refresh token out from under the real login.

    The same directory named twice is polled once, so one credential is never
    refreshed twice in a run.
    """
    entries = home_entries((os.environ if environ is None else environ).get(CODEX_HOMES_ENV, ""))
    if not entries:
        return [CodexHome(CODEX_DEFAULT_HOME)]

    default = CODEX_DEFAULT_HOME.expanduser().resolve()
    homes, seen = [], set()
    for label, entry in entries:
        root = Path(entry).expanduser().resolve()
        if root in seen:
            continue
        seen.add(root)
        homes.append(CodexHome(root, "" if root == default else root.name.lstrip(".") or str(root),
                               label))
    return homes


def jwt_exp(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part)).get("exp")
    except Exception:
        return None


def codex_plan_label(plan_type):
    if not isinstance(plan_type, str) or not plan_type:
        return ""
    return CODEX_PLAN_LABELS.get(plan_type.lower(), plan_type.replace("_", " ").title())


def codex_window_identity(seconds):
    """A Codex Window's identity and, when it is one no label set can name, the
    label to show it under. Both come from the Window's length — nothing that
    appears on screen is ever part of an identity.

    Returns (None, "") for a length we cannot read at all: a Window we cannot
    identify is left out rather than merged into whatever else has no identity.
    """
    length = finite_number(seconds)
    if length is None:
        return None, ""
    length = int(length)
    known = WINDOW_IDS.get(length)
    if known:
        return known, ""
    return f"{LENGTH_WINDOW_PREFIX}{length}", compact_length(length)


def compact_length(seconds):
    """A Window's length in its largest whole unit: 5d, 3h, 20m."""
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def codex_reading(body, plan, now):
    """Codex's usage response as a Usage Reading.

    Its Reset is a bare epoch from an undocumented endpoint — the response even
    carries the same moment in relative form beside it — so it goes through the
    same validation Claude's does rather than being written down unchallenged.
    """
    windows = []
    rate_limit = body.get("rate_limit") or {}
    for section in ("primary_window", "secondary_window"):
        window = rate_limit.get(section)
        if not isinstance(window, dict):
            continue
        window_id, label = codex_window_identity(window.get("limit_window_seconds"))
        if window_id is None:
            continue
        share = used_share(window.get("used_percent"), window_id)
        if share is None:
            continue
        windows.append(Window(id=window_id, used=share, label=label,
                              resets_at=plausible_reset(window.get("reset_at"), now, window_id)))
    return UsageReading(provider="codex", plan=plan,
                        windows=tuple(windows), captured_at=now)


def codex_refresh(refresh_token):
    body = "&".join([
        "grant_type=refresh_token",
        f"client_id={CODEX_CLIENT_ID}",
        f"refresh_token={urllib.request.quote(refresh_token, safe='')}",
    ]).encode()
    req = urllib.request.Request(
        CODEX_REFRESH_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


class CodexPersistError(Exception):
    """A rotated Codex credential could not be written back to auth.json."""


class CodexConcurrentRotation(CodexPersistError):
    """Codex rotated the credential itself while our refresh was in flight, so the
    file already holds a newer, live token that must not be overwritten. Nothing is
    broken in this case — it is the one persist failure that needs no user action."""


def token_fingerprint(token):
    """A stable, non-reversible handle for a credential, so we can tell whether the
    stored one has changed without writing a second copy of the secret to disk."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def codex_persist_rotation(home, expected_refresh, new_access, new_refresh):
    """Record a rotated Codex credential in the home's auth.json, preserving every
    other field in the file.

    Raises rather than failing quietly. By the time this runs the server has already
    rotated the refresh token, so the copy still on disk is dead: swallowing a
    failure here logs the user out of Codex with nothing to connect it to.

    Re-reads first and refuses to write when the stored refresh token is no longer
    `expected_refresh` — that means Codex rotated the credential itself while our
    request was in flight, and its newer token, not ours, is what the file should
    keep. That case raises `CodexConcurrentRotation` so callers can tell it apart
    from a genuine loss; everything else raises `CodexPersistError`."""
    cur = load_json(home.auth_file)
    if not isinstance(cur, dict):
        raise CodexPersistError(f"{home.auth_file} is missing or unreadable as JSON")

    stored_refresh = (cur.get("tokens") or {}).get("refresh_token")
    if stored_refresh != expected_refresh:
        raise CodexConcurrentRotation("auth.json already holds a newer credential")

    tokens = cur.setdefault("tokens", {})
    tokens["access_token"] = new_access
    if new_refresh:
        tokens["refresh_token"] = new_refresh
    cur["last_refresh"] = stamp().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        write_atomic(home.auth_file, cur)
    except Exception as error:
        raise CodexPersistError(f"could not write {home.auth_file}: {error}") from error


def codex_record_rotation_loss(home, refresh_token):
    """Remember that this credential was spent without its replacement being saved,
    so later runs keep saying so instead of decaying into a generic refresh error.
    Stores only a fingerprint, never the token."""
    try:
        write_atomic(home.rotation_lost, {
            "consumedRefreshSha256": token_fingerprint(refresh_token),
            "at": stamp().strftime(ISO_SECONDS),
        })
    except Exception as error:
        log(f"{home.tag}: could not record the lost rotation ({error})")


def codex_rotation_still_lost(home, refresh_token):
    """True when an earlier run burned a rotation it could not save and the stored
    credential is still that dead one. Clears the marker as soon as the credential
    changes, so `codex login` is the whole recovery procedure."""
    marker = load_json(home.rotation_lost)
    if not isinstance(marker, dict):
        return False
    if marker.get("consumedRefreshSha256") == token_fingerprint(refresh_token):
        return True
    try:
        home.rotation_lost.unlink()
    except OSError:
        pass
    log(f"{home.tag}: credential changed since the lost rotation — clearing the marker")
    return False


def codex_recover(home, why, now):
    return recover_card("codex", why, home.card, home.reading, now, home.account)


def codex_poll(home, now):
    auth = load_json(home.auth_file)
    tokens = (auth or {}).get("tokens") or {}
    access = tokens.get("access_token") or ""
    account_id = tokens.get("account_id")
    refresh_token = tokens.get("refresh_token")
    if not access:
        # The same situation as Claude's expired token: we cannot read usage, so
        # the Card decays from what we last knew rather than standing frozen at a
        # share that may have reset hours ago.
        codex_recover(home, "no access token", now)
        return

    if codex_rotation_still_lost(home, refresh_token):
        log(f"{home.tag}: an earlier rotation was lost and the stored credential is still "
            "the invalidated one — run `codex login` to recover")
        codex_recover(home, "the stored credential is invalid", now)
        return

    exp = jwt_exp(access)
    if isinstance(exp, (int, float)) and exp - now <= REFRESH_BUFFER_S and refresh_token:
        refreshed = None
        try:
            refreshed = codex_refresh(refresh_token)
        except urllib.error.HTTPError as e:
            log(f"{home.tag}: refresh HTTP {e.code} — trying existing token")
        except Exception as e:
            log(f"{home.tag}: refresh failed ({e}) — trying existing token")
        if refreshed is not None and not isinstance(refreshed, dict):
            log(f"{home.tag}: refresh returned an unexpected shape — trying existing token")
            refreshed = None

        new_access = (refreshed or {}).get("access_token")
        if new_access:
            # The server has rotated the credential by now, so the copy on disk is
            # spent. Everything below hangs on recording that.
            try:
                codex_persist_rotation(home, refresh_token, new_access, refreshed.get("refresh_token"))
            except CodexConcurrentRotation:
                # Codex refreshed at the same moment and what it wrote is the live
                # credential. Nothing is broken — pick that up and carry on.
                stored = (load_json(home.auth_file) or {}).get("tokens") or {}
                access = stored.get("access_token") or access
                log(f"{home.tag}: credential was rotated by Codex mid-refresh — using the stored token")
            except CodexPersistError as e:
                codex_record_rotation_loss(home, refresh_token)
                log(f"{home.tag}: token rotated but NOT saved ({e}) — the credential on disk "
                    "is now invalid; run `codex login` to recover")
                codex_recover(home, "the rotated credential could not be saved", now)
                return
            else:
                access = new_access
                log(f"{home.tag}: token refreshed")

    headers = {"Authorization": f"Bearer {access}"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    ok, body = fetch_usage(CODEX_USAGE_URL, headers, functools.partial(codex_recover, home), now)
    if not ok:
        return

    publish("codex", "usage response", home.card, home.reading,
            codex_reading(body, codex_plan_label(body.get("plan_type")), now), now, home.account)


# ----------------------------- main -----------------------------

def main():
    # One clock per tick, read once here at the single place a poll begins and
    # handed to both Providers, so a Card, its persisted Reading and its
    # countdowns are all derived from the same instant. The side-effect
    # timestamps (log lines, last_refresh, the rotation-loss marker) read the
    # real wall clock via stamp() instead — see decision 3 of the design.
    now = datetime.now(timezone.utc).timestamp()
    polls = [(home.tag, functools.partial(claude_poll, home)) for home in claude_homes()]
    polls += [(home.tag, functools.partial(codex_poll, home)) for home in codex_homes()]
    # One login failing must not cost the others their Card.
    for name, fn in polls:
        try:
            fn(now)
        except Exception as e:
            log(f"{name}: unexpected error ({e})")
