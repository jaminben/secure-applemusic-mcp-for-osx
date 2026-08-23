"""Once-a-day "is there a newer release?" check against GitHub.

Three constraints shaped this, and they are worth stating because each one
rules out an obvious design:

1. **This process holds the Automation grant.** It can drive Music.app. So the
   checker NEVER downloads or applies anything — it reports a version and a
   URL, and a human decides. An auto-updater here would turn a compromised
   release channel into code execution with Music control, which is exactly the
   threat model the rest of this fork is built against.

2. **Everything the API returns is attacker-controlled if the repo is
   compromised**, and it ends up on a terminal or in a notification. So tags are
   *validated* against a version shape rather than merely escaped, URLs must be
   under this repo, and free text is stripped of control characters and capped.
   Nothing from the response is ever executed, and no URL from the response is
   ever opened automatically.

3. **Users chose this fork for its network posture.** So: opt-out env var, one
   documented endpoint pair, at most one check per day, short timeout, and total
   silence on every failure. A checker that nags or blocks would push people to
   disable it, and then it protects nobody when it matters.

The security path is the reason this exists at all. A plain "0.2.0 is out"
notice is suppressed after the user has seen it once (see ``should_notify``) —
otherwise a daily check becomes a daily nag. A *published advisory that names
the running version as vulnerable* is not suppressed: it re-notifies every day
until the installed version is out of the affected range.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

from . import __version__
from .paths import cache_dir

REPO = "jaminben/secure-applemusic-mcp-for-osx"
_API = "https://api.github.com"
RELEASES_URL = f"https://github.com/{REPO}/releases"
_STATE_NAME = "update-check.json"

CHECK_INTERVAL = 24 * 60 * 60
_TIMEOUT = 5
_OPT_OUT = "APPLEMUSIC_MCP_NO_UPDATE_CHECK"

# A tag we will consent to display or compare. Anything else is discarded
# outright — a "version" that isn't shaped like one is either a repo we don't
# understand or a repo someone else is driving.
# Components are length-capped: an absurd "v999…999" would otherwise parse to a
# huge int, compare as newer forever, and nag every day about a release that
# does not exist. Six digits is more version than anyone ships.
_VERSION_RE = re.compile(r"^v?(\d{1,6}(?:\.\d{1,6}){0,3})(?:[-+]([0-9A-Za-z.\-]{1,32}))?$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# --------------------------------------------------------------------------
# versions


def parse_version(text: str) -> Optional[tuple]:
    """``"v0.2.0"`` -> a sortable key, or ``None`` if it isn't a version.

    Returning None for junk is the point: callers treat "unparseable" as "say
    nothing", never as "newer" (which is the direction that nags) and never as
    "older" (which is the direction that hides a security notice).
    """
    if not isinstance(text, str):
        return None
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    nums = tuple(int(p) for p in m.group(1).split("."))
    nums = nums + (0,) * (4 - len(nums))
    pre = m.group(2)
    # A prerelease sorts BELOW the same numbers without one: 0.2.0-rc1 < 0.2.0.
    return (nums, 0 if pre else 1, pre or "")


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    return a > b


def _range_matches(current: str, spec: str) -> bool:
    """Does ``current`` fall in an advisory range like ``">=0.1.0, <0.2.1"``?

    Unknown operators and unparseable bounds make the whole term fail CLOSED
    (returns False). An advisory we can't parse must not be reported as "you
    are affected" — a false security alarm trains people to ignore real ones.
    """
    key = parse_version(current)
    if key is None or not isinstance(spec, str):
        return False
    terms = [t.strip() for t in spec.split(",") if t.strip()]
    if not terms:
        return False
    for term in terms:
        m = re.match(r"^(>=|<=|==|=|>|<)?\s*(\S+)$", term)
        if not m:
            return False
        op, bound_text = m.group(1) or "=", m.group(2)
        bound = parse_version(bound_text)
        if bound is None:
            return False
        if op == ">=" and not key >= bound:
            return False
        if op == ">" and not key > bound:
            return False
        if op == "<=" and not key <= bound:
            return False
        if op == "<" and not key < bound:
            return False
        if op in ("=", "==") and key != bound:
            return False
    return True


# --------------------------------------------------------------------------
# untrusted text


def _clean(text: Any, limit: int = 200) -> str:
    """Make a remote string safe to print to a terminal or a notification.

    Control characters go first: an unstripped ESC lets a compromised release
    repaint the terminal, and an unstripped newline lets it forge additional
    lines of our own output.
    """
    if not isinstance(text, str):
        return ""
    out = _CONTROL_RE.sub(" ", text).strip()
    return out[: limit - 1] + "…" if len(out) > limit else out


def _repo_url(url: Any, fallback: str) -> str:
    """Only ever hand back a URL that lives under this repo."""
    prefix = f"https://github.com/{REPO}/"
    return url if isinstance(url, str) and url.startswith(prefix) else fallback


# --------------------------------------------------------------------------
# state


def state_path():
    return cache_dir() / _STATE_NAME


def _load_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    path = state_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def disabled() -> bool:
    return os.environ.get(_OPT_OUT, "").strip() not in ("", "0", "false", "no")


def due(state: Optional[dict] = None, now: Optional[float] = None) -> bool:
    state = _load_state() if state is None else state
    now = time.time() if now is None else now
    last = state.get("last_check")
    if not isinstance(last, (int, float)):
        return True
    # A clock that jumped backwards must not park the checker for a decade.
    return not (0 <= now - last < CHECK_INTERVAL)


# --------------------------------------------------------------------------
# the network half


def _get(path: str) -> Any:
    import requests

    r = requests.get(
        f"{_API}/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"secure-applemusic-mcp/{__version__}",
        },
        timeout=_TIMEOUT,
    )
    if r.status_code == 404:
        return None  # no releases published yet — a real state, not an error
    r.raise_for_status()
    return r.json()


def _fetch_latest() -> Optional[str]:
    data = _get(f"repos/{REPO}/releases/latest")
    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name")
    return tag if parse_version(tag or "") else None


def _fetch_advisory(current: str) -> Optional[dict]:
    """The most severe published advisory whose range covers ``current``."""
    data = _get(f"repos/{REPO}/security-advisories?state=published&per_page=100")
    if not isinstance(data, list):
        return None
    rank = {"critical": 3, "high": 2, "moderate": 1, "low": 0}
    best, best_rank = None, -1
    for adv in data:
        if not isinstance(adv, dict):
            continue
        vulns = adv.get("vulnerabilities")
        vulns = vulns if isinstance(vulns, list) else []
        if not any(
            isinstance(v, dict) and _range_matches(current, v.get("vulnerable_version_range", ""))
            for v in vulns
        ):
            continue
        severity = adv.get("severity") if adv.get("severity") in rank else "moderate"
        if rank[severity] > best_rank:
            patched = next(
                (
                    v.get("patched_versions")
                    for v in vulns
                    if isinstance(v, dict) and parse_version(str(v.get("patched_versions") or ""))
                ),
                None,
            )
            best, best_rank = (
                {
                    "id": _clean(adv.get("ghsa_id"), 64),
                    "severity": severity,
                    "summary": _clean(adv.get("summary"), 200),
                    "url": _repo_url(adv.get("html_url"), f"https://github.com/{REPO}/security"),
                    "patched": _clean(patched, 32),
                },
                rank[severity],
            )
    return best


def check(force: bool = False, current: Optional[str] = None) -> dict:
    """Run the check if it's due. Never raises; never blocks for long.

    Returns the current view either way, so a caller that hits the daily cache
    still gets something to display.
    """
    current = current or __version__
    state = _load_state()

    if disabled():
        return {"status": "disabled", "current": current}
    if not (force or due(state)):
        return {**_result_from(state, current), "status": "cached"}

    try:
        latest = _fetch_latest()
        advisory = _fetch_advisory(current)
    except Exception:  # noqa: BLE001 - a failed check is silence, never an error
        state["last_check"] = time.time()
        _save_state(state)
        return {**_result_from(state, current), "status": "unreachable"}

    state["last_check"] = time.time()
    state["latest"] = latest
    state["advisory"] = advisory
    _save_state(state)
    return {**_result_from(state, current), "status": "checked"}


def _result_from(state: dict, current: str) -> dict:
    latest = state.get("latest")
    latest = latest if isinstance(latest, str) and parse_version(latest) else None
    advisory = state.get("advisory") if isinstance(state.get("advisory"), dict) else None
    return {
        "current": current,
        "latest": latest,
        "update_available": bool(latest and is_newer(latest, current)),
        "advisory": advisory,
        "url": RELEASES_URL,
        "notified": state.get("notified"),
    }


# --------------------------------------------------------------------------
# telling the user, at most as often as they need to hear it


def should_notify(result: dict) -> bool:
    """Push notices only. ``status`` shows everything regardless.

    A routine update is announced once per version. A security advisory that
    covers the running version is announced every time the check runs, because
    "you already dismissed this" is not a reason to stop saying it.
    """
    if result.get("advisory"):
        return True
    if not result.get("update_available"):
        return False
    return result.get("notified") != result.get("latest")


def mark_notified(result: dict) -> None:
    state = _load_state()
    state["notified"] = result.get("latest")
    _save_state(state)


def summary_lines(result: dict) -> list[str]:
    """Human-readable lines for ``status``. Empty when there is nothing to say."""
    adv = result.get("advisory")
    lines: list[str] = []
    if adv:
        sev = str(adv.get("severity", "")).upper()
        lines.append(f"SECURITY ADVISORY ({sev}) affects your version {result['current']}")
        if adv.get("summary"):
            lines.append(f"  {adv['summary']}")
        if adv.get("patched"):
            lines.append(f"  Fixed in {adv['patched']}")
        lines.append(f"  {adv.get('url')}")
    if result.get("update_available"):
        # Tags carry a leading "v", the installed version doesn't. Strip it so the
        # two sides of the arrow are comparable at a glance.
        latest = str(result["latest"]).lstrip("v")
        lines.append(f"Update available: {result['current']} -> {latest}")
        lines.append(f"  {result['url']}")
    return lines


def notify_macos(result: dict) -> bool:
    """Post a macOS notification. Returns whether it was posted.

    Routed through ``applescript.run_applescript`` rather than spawning
    ``osascript`` here: this fork keeps exactly one process-execution call site
    so the executable surface stays auditable, and the capability-invariant
    suite enforces it. A second call site would be a real capability
    regression, not a style preference.

    Deliberately a notification and not a modal: a background LaunchAgent that
    seizes focus is indistinguishable, to the user, from the thing they were
    afraid of when they read our permissions doc. The tradeoff is that
    ``display notification`` has no clickable action, so the text has to name
    where to go — and the same information is waiting in ``status``.
    """
    from .applescript import _escape_for_applescript, run_applescript

    adv = result.get("advisory")
    if adv:
        title = f"Apple Music MCP — {str(adv.get('severity', '')).upper()} security update"
        body = adv.get("summary") or "A published advisory affects your installed version."
        if adv.get("patched"):
            body = f"{body} Fixed in {adv['patched']}."
    elif result.get("update_available"):
        title = "Apple Music MCP — update available"
        body = f"Version {result['latest']} is out (you have {result['current']})."
    else:
        return False

    # Everything interpolated below has already been through _clean (control
    # characters stripped, length capped) and, for versions, parse_version.
    body = _escape_for_applescript(f"{_clean(body, 180)} See {RELEASES_URL}")
    title = _escape_for_applescript(_clean(title, 100))
    script = f'display notification "{body}" with title "{title}"'
    if adv:
        script += ' sound name "Basso"'
    ok, _out = run_applescript(script)
    return bool(ok)
