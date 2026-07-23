#!/usr/bin/env python3
"""RunCat Neo usage poller — Claude Code + Codex, one file.

Reads the OAuth credentials already on this machine and calls each provider's
dedicated usage endpoint (a plain metadata GET — no model inference, no token
cost) to write real, account-wide rate-limit numbers into the RunCat Neo
snapshots. Designed to run on a launchd interval (every ~5 min).

Card shape:
- title carries the plan, e.g. "Claude Code Max 20x" / "Codex Pro Lite".
- each rate-limit window is two rows: the used% (with the bar) and, underneath
  it, a bar-less "reset in <time>" line so the countdown isn't cramped next to
  the percentage. Windows are "Session" (5h) and "Weekly" (7d).

Design (safe / read-mostly):
- Codex tokens live in ~/.codex/auth.json (a plain file). We may refresh an
  expired token and write it back to that file — the same mechanism Codex uses.
- Claude tokens live in the macOS Keychain. We READ the access token via the
  Apple-signed `security` CLI (no GUI prompt) but NEVER write the keychain: an
  unsigned script can't do an ACL-preserving SecItemUpdate the way the signed
  apps do, and a `security -U` write could lock Claude Code out of its own
  credential. So when the Claude token is valid we poll live usage; when it is
  expired we do NOT refresh — instead we fall back to a local reset-time
  computation (zeroing any window whose absolute reset epoch has passed) using
  reset times captured on the last successful poll. Claude Code refreshes its
  own token whenever you use it, and the next poll goes live again.

A provider that errors leaves its existing snapshot untouched (last-good).
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
NOW = datetime.now(timezone.utc)
NOW_EPOCH = NOW.timestamp()
NOW_MS = NOW_EPOCH * 1000
NOW_ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
REFRESH_BUFFER_S = 5 * 60  # refresh (Codex) / treat-expired margin


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


LANG = detect_lang()
STRINGS = {
    "ko": {"session": "현재 세션", "weekly": "주간 한도", "reset": "재설정"},
    "en": {"session": "Session", "weekly": "Weekly", "reset": "reset"},
}
T = STRINGS[LANG]
RESET_ROW_TITLE = T["reset"]  # bar-less sub-row under each window

# ---- Claude constants (from Claude Code / OpenUsage) ----
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_USAGE_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "anthropic-beta": "oauth-2025-04-20",
    "User-Agent": "claude-code/2.1.69",
}
CLAUDE_SNAPSHOT = HOME / ".claude" / "runcat-usage.json"
CLAUDE_SIDECAR = HOME / ".claude" / "runcat-reset-state.json"

# ---- Codex constants ----
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_REFRESH_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_FILE = HOME / ".codex" / "auth.json"
CODEX_SNAPSHOT = HOME / ".codex" / "runcat-usage.json"
CODEX_SIDECAR = HOME / ".codex" / "runcat-reset-state.json"
CODEX_ROTATION_LOST = HOME / ".codex" / "runcat-rotation-lost.json"

CODEX_PLAN_LABELS = {
    "free": "Free", "plus": "Plus", "pro": "Pro 20x", "prolite": "Pro 5x",
    "team": "Team", "business": "Business", "enterprise": "Enterprise",
}
# window length (seconds) -> row label
WINDOW_LABELS = {18000: T["session"], 604800: T["weekly"]}


# ----------------------------- helpers -----------------------------

def log(msg):
    print(f"[{NOW_ISO}] {msg}", file=sys.stderr)


def load_json(path):
    try:
        with Path(path).open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
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


def fmt_duration(seconds):
    """Compact 'time left' in the active language: 4일 15시간 / 4d 15h etc."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if LANG == "ko":
        if days:
            return f"{days}일 {hours}시간"
        if hours:
            return f"{hours}시간 {minutes}분"
        if minutes:
            return f"{minutes}분"
        return "1분 미만"
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def window_rows(title, used, resets_at_epoch):
    """A window's used% row (with bar) plus a bar-less 'reset in <time>' row."""
    if not isinstance(used, (int, float)):
        return []
    v = max(0.0, min(float(used), 100.0))
    rows = [{"title": title, "formattedValue": f"{v:g}%", "normalizedValue": round(v / 100, 4)}]
    if isinstance(resets_at_epoch, (int, float)) and resets_at_epoch - NOW_EPOCH > 0:
        dur = fmt_duration(resets_at_epoch - NOW_EPOCH)
        rows.append({"title": RESET_ROW_TITLE, "formattedValue": f"{dur} 후" if LANG == "ko" else dur})
    return rows


def iso_to_epoch(s):
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def join_blocks(blocks):
    """Concatenate per-window row groups, inserting a blank line between groups.
    RunCat has no spacer row, so we append a trailing newline to the previous
    group's last value — this renders as an empty line above the next window
    while keeping window titles clean (so reset-fallback matching still works)."""
    metrics = []
    for i, rows in enumerate(blocks):
        if i > 0 and metrics:
            last = dict(metrics[-1])
            last["formattedValue"] = last["formattedValue"] + "\n"
            metrics[-1] = last
        metrics.extend(rows)
    return metrics


def bar_two(*pcts):
    """Menu-bar text from up to two percentages, single line (RunCat's menu-bar
    view is single-line; a newline just gets truncated). e.g. '18% · 39%'."""
    parts = [f"{p:g}%" for p in pcts if isinstance(p, (int, float))]
    return " · ".join(parts) if parts else None


def finalize(title, symbol, metrics, bar_value=None):
    """Build a snapshot. bar_value = menu-bar text; if omitted, falls back to the
    single most-used window percentage."""
    rows = [m for m in metrics if m is not None]
    if bar_value is None:
        bar = max((m["normalizedValue"] for m in rows if "normalizedValue" in m), default=None)
        bar_value = f"{bar * 100:g}%" if bar is not None else None
    snap = {"title": title, "symbol": symbol, "metrics": rows, "lastUpdatedDate": NOW_ISO}
    if bar_value is not None:
        snap["metricsBarValue"] = bar_value
    return snap


def apply_reset_fallback(snapshot_path, sidecar_path):
    """When we can't poll: zero any window whose captured reset epoch has passed
    (and blank its 'reset in' sub-row). Rewrites only if something changed."""
    snapshot = load_json(snapshot_path)
    resets = load_json(sidecar_path)
    if not isinstance(snapshot, dict) or not isinstance(resets, dict):
        return
    metrics = snapshot.get("metrics", [])
    changed = False
    for i, metric in enumerate(metrics):
        if not isinstance(metric, dict):
            continue
        resets_at = resets.get(metric.get("title"))
        if not isinstance(resets_at, (int, float)) or NOW_EPOCH < resets_at:
            continue
        if metric.get("normalizedValue") == 0:
            continue  # already reset
        metric["formattedValue"] = "0%"
        metric["normalizedValue"] = 0
        changed = True
        nxt = metrics[i + 1] if i + 1 < len(metrics) else None
        if isinstance(nxt, dict) and "normalizedValue" not in nxt and nxt.get("formattedValue") != "—":
            nxt["formattedValue"] = "—"
    if changed:
        snapshot["lastUpdatedDate"] = NOW_ISO
        write_atomic(snapshot_path, snapshot)


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


def claude_read_token():
    """Read the Claude OAuth blob from the login Keychain via the signed `security`
    CLI (no GUI prompt). Times out rather than hanging launchd if a prompt ever
    appears. Returns the parsed claudeAiOauth dict or None."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        log("claude: keychain read timed out (prompt?) — skipping live poll")
        return None
    if out.returncode != 0:
        return None
    try:
        return (json.loads(out.stdout) or {}).get("claudeAiOauth") or {}
    except json.JSONDecodeError:
        return None


def claude_poll():
    oauth = claude_read_token()
    token = (oauth or {}).get("accessToken") or ""
    expires_at = (oauth or {}).get("expiresAt")  # epoch ms
    token_ok = bool(token) and (not isinstance(expires_at, (int, float)) or expires_at > NOW_MS)

    if not token_ok:
        # Expired / unreadable: never refresh (keychain-write unsafe). Fall back.
        apply_reset_fallback(CLAUDE_SNAPSHOT, CLAUDE_SIDECAR)
        log("claude: token expired/absent — reset-fallback applied")
        return

    headers = dict(CLAUDE_USAGE_HEADERS, Authorization=f"Bearer {token.strip()}")
    try:
        status, body = http_get_json(CLAUDE_USAGE_URL, headers)
    except urllib.error.HTTPError as e:
        apply_reset_fallback(CLAUDE_SNAPSHOT, CLAUDE_SIDECAR)
        log(f"claude: usage HTTP {e.code} — reset-fallback applied")
        return
    except Exception as e:
        log(f"claude: usage fetch failed ({e}) — keeping last-good")
        return

    title = ("Claude Code " + claude_plan_label(oauth)).strip()
    blocks, resets = [], {}

    def add(label, used, reset_iso):
        ep = iso_to_epoch(reset_iso)
        rows = window_rows(label, used, ep)
        if rows:
            blocks.append(rows)
            if ep is not None:
                resets[label] = int(ep)

    # `limits[]` uniformly carries session, weekly, and per-model weekly-scoped
    # caps (e.g. Fable); fall back to the top-level windows if it's absent.
    session_pct = weekly_pct = None
    limits = body.get("limits")
    if isinstance(limits, list) and limits:
        kind_labels = {"session": T["session"], "weekly_all": T["weekly"]}
        for lim in limits:
            if not isinstance(lim, dict):
                continue
            kind = lim.get("kind")
            if kind == "weekly_scoped":
                label = (((lim.get("scope") or {}).get("model") or {}).get("display_name"))
            else:
                label = kind_labels.get(kind)
            if kind == "session":
                session_pct = lim.get("percent")
            elif kind == "weekly_all":
                weekly_pct = lim.get("percent")
            if label:
                add(label, lim.get("percent"), lim.get("resets_at"))
    else:
        s, w = body.get("five_hour") or {}, body.get("seven_day") or {}
        session_pct, weekly_pct = s.get("utilization"), w.get("utilization")
        add(T["session"], s.get("utilization"), s.get("resets_at"))
        add(T["weekly"], w.get("utilization"), w.get("resets_at"))

    metrics = join_blocks(blocks)
    if not metrics:
        log("claude: usage response had no windows — keeping last-good")
        return

    write_atomic(CLAUDE_SNAPSHOT, finalize(title, "staroflife", metrics, bar_two(session_pct, weekly_pct)))
    if resets:
        write_atomic(CLAUDE_SIDECAR, resets)
    log("claude: live usage written")


# ----------------------------- Codex -----------------------------

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


def codex_window_label(seconds):
    if seconds in WINDOW_LABELS:
        return WINDOW_LABELS[seconds]
    if not isinstance(seconds, (int, float)):
        return None
    seconds = int(seconds)
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


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


def codex_persist_rotation(expected_refresh, new_access, new_refresh):
    """Record a rotated Codex credential in ~/.codex/auth.json, preserving every
    other field in the file.

    Raises rather than failing quietly. By the time this runs the server has already
    rotated the refresh token, so the copy still on disk is dead: swallowing a
    failure here logs the user out of Codex with nothing to connect it to.

    Re-reads first and refuses to write when the stored refresh token is no longer
    `expected_refresh` — that means Codex rotated the credential itself while our
    request was in flight, and its newer token, not ours, is what the file should
    keep. That case raises `CodexConcurrentRotation` so callers can tell it apart
    from a genuine loss; everything else raises `CodexPersistError`."""
    cur = load_json(CODEX_AUTH_FILE)
    if not isinstance(cur, dict):
        raise CodexPersistError(f"{CODEX_AUTH_FILE} is missing or unreadable as JSON")

    stored_refresh = (cur.get("tokens") or {}).get("refresh_token")
    if stored_refresh != expected_refresh:
        raise CodexConcurrentRotation("auth.json already holds a newer credential")

    tokens = cur.setdefault("tokens", {})
    tokens["access_token"] = new_access
    if new_refresh:
        tokens["refresh_token"] = new_refresh
    cur["last_refresh"] = NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        write_atomic(CODEX_AUTH_FILE, cur)
    except Exception as error:
        raise CodexPersistError(f"could not write {CODEX_AUTH_FILE}: {error}") from error


def codex_record_rotation_loss(refresh_token):
    """Remember that this credential was spent without its replacement being saved,
    so later runs keep saying so instead of decaying into a generic refresh error.
    Stores only a fingerprint, never the token."""
    try:
        write_atomic(CODEX_ROTATION_LOST, {
            "consumedRefreshSha256": token_fingerprint(refresh_token),
            "at": NOW_ISO,
        })
    except Exception as error:
        log(f"codex: could not record the lost rotation ({error})")


def codex_rotation_still_lost(refresh_token):
    """True when an earlier run burned a rotation it could not save and the stored
    credential is still that dead one. Clears the marker as soon as the credential
    changes, so `codex login` is the whole recovery procedure."""
    marker = load_json(CODEX_ROTATION_LOST)
    if not isinstance(marker, dict):
        return False
    if marker.get("consumedRefreshSha256") == token_fingerprint(refresh_token):
        return True
    try:
        CODEX_ROTATION_LOST.unlink()
    except OSError:
        pass
    log("codex: credential changed since the lost rotation — clearing the marker")
    return False


def codex_poll():
    auth = load_json(CODEX_AUTH_FILE)
    tokens = (auth or {}).get("tokens") or {}
    access = tokens.get("access_token") or ""
    account_id = tokens.get("account_id")
    refresh_token = tokens.get("refresh_token")
    if not access:
        log("codex: no access token — skipping")
        return

    if codex_rotation_still_lost(refresh_token):
        log("codex: an earlier rotation was lost and the stored credential is still the "
            "invalidated one — run `codex login` to recover")
        apply_reset_fallback(CODEX_SNAPSHOT, CODEX_SIDECAR)
        return

    exp = jwt_exp(access)
    if isinstance(exp, (int, float)) and exp - NOW_EPOCH <= REFRESH_BUFFER_S and refresh_token:
        refreshed = None
        try:
            refreshed = codex_refresh(refresh_token)
        except urllib.error.HTTPError as e:
            log(f"codex: refresh HTTP {e.code} — trying existing token")
        except Exception as e:
            log(f"codex: refresh failed ({e}) — trying existing token")
        if refreshed is not None and not isinstance(refreshed, dict):
            log("codex: refresh returned an unexpected shape — trying existing token")
            refreshed = None

        new_access = (refreshed or {}).get("access_token")
        if new_access:
            # The server has rotated the credential by now, so the copy on disk is
            # spent. Everything below hangs on recording that.
            try:
                codex_persist_rotation(refresh_token, new_access, refreshed.get("refresh_token"))
            except CodexConcurrentRotation:
                # Codex refreshed at the same moment and what it wrote is the live
                # credential. Nothing is broken — pick that up and carry on.
                stored = (load_json(CODEX_AUTH_FILE) or {}).get("tokens") or {}
                access = stored.get("access_token") or access
                log("codex: credential was rotated by Codex mid-refresh — using the stored token")
            except CodexPersistError as e:
                codex_record_rotation_loss(refresh_token)
                log(f"codex: token rotated but NOT saved ({e}) — the credential on disk "
                    "is now invalid; run `codex login` to recover")
                apply_reset_fallback(CODEX_SNAPSHOT, CODEX_SIDECAR)
                return
            else:
                access = new_access
                log("codex: token refreshed")

    headers = {"Authorization": f"Bearer {access}"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    try:
        status, body = http_get_json(CODEX_USAGE_URL, headers)
    except urllib.error.HTTPError as e:
        apply_reset_fallback(CODEX_SNAPSHOT, CODEX_SIDECAR)
        log(f"codex: usage HTTP {e.code} — reset-fallback applied")
        return
    except Exception as e:
        log(f"codex: usage fetch failed ({e}) — keeping last-good")
        return

    title = ("Codex " + codex_plan_label(body.get("plan_type"))).strip()
    blocks, resets = [], {}
    session_pct = weekly_pct = None
    rate_limit = body.get("rate_limit") or {}
    for window in (rate_limit.get("primary_window"), rate_limit.get("secondary_window")):
        if not isinstance(window, dict):
            continue
        secs = window.get("limit_window_seconds")
        used = window.get("used_percent")
        if secs == 18000:
            session_pct = used
        elif secs == 604800:
            weekly_pct = used
        row = codex_window_label(secs)
        reset_at = window.get("reset_at")
        rows = window_rows(row, used, reset_at) if row else []
        if rows:
            blocks.append(rows)
            if isinstance(reset_at, (int, float)):
                resets[row] = int(reset_at)
    metrics = join_blocks(blocks)
    if not metrics:
        log("codex: usage response had no windows — keeping last-good")
        return

    write_atomic(CODEX_SNAPSHOT, finalize(title, "camera.aperture", metrics, bar_two(session_pct, weekly_pct)))
    if resets:
        write_atomic(CODEX_SIDECAR, resets)
    log("codex: live usage written")


# ----------------------------- main -----------------------------

def main():
    for name, fn in (("claude", claude_poll), ("codex", codex_poll)):
        try:
            fn()
        except Exception as e:
            log(f"{name}: unexpected error ({e})")


if __name__ == "__main__":
    main()
