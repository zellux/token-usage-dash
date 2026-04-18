#!/usr/bin/env python3
"""
Fetch subscription plan usage for Claude and OpenAI Codex.

Claude: uses OAuth token from ~/.claude/.credentials.json
OpenAI: scrapes chatgpt.com/codex/settings/usage via Playwright using browser cookies

OpenAI cookie setup (one-time):
  1. Log into chatgpt.com in your browser
  2. Open DevTools → Application → Cookies → https://chatgpt.com
  3. Copy the full Cookie header value (or individual cookies) into:
       ~/.config/token-usage-dash/openai-cookies.json
     Format: {"__Secure-next-auth.session-token": "...", "_uasid": "...", ...}
"""

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
TOKEN_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

OPENAI_USAGE_URL = "https://chatgpt.com/codex/cloud/settings/analytics#usage"
OPENAI_COOKIES_PATH = Path.home() / ".config" / "token-usage-dash" / "openai-cookies.json"


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"No credentials found at {CREDENTIALS_PATH}. "
            "Run `claude` to authenticate first."
        )
    with open(CREDENTIALS_PATH) as f:
        data = json.load(f)
    return data["claudeAiOauth"]


def save_credentials(creds: dict) -> None:
    with open(CREDENTIALS_PATH) as f:
        data = json.load(f)
    data["claudeAiOauth"].update(creds)
    with open(CREDENTIALS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _refresh_token(refresh_token: str) -> dict:
    resp = requests.post(
        TOKEN_ENDPOINT,
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_claude_usage(access_token: str) -> dict:
    resp = requests.get(
        USAGE_ENDPOINT,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
        timeout=10,
    )
    if resp.status_code == 429:
        raise RuntimeError("Rate limited by usage endpoint. Try again in a few minutes.")
    resp.raise_for_status()
    return resp.json()


def get_claude_usage() -> dict:
    creds = load_credentials()
    access_token = creds["accessToken"]

    try:
        return _fetch_claude_usage(access_token)
    except requests.HTTPError as e:
        if e.response.status_code != 401:
            raise
        new = _refresh_token(creds["refreshToken"])
        save_credentials({
            "accessToken": new["access_token"],
            "refreshToken": new.get("refresh_token", creds["refreshToken"]),
        })
        return _fetch_claude_usage(new["access_token"])


# ---------------------------------------------------------------------------
# OpenAI Codex — browser scrape via Playwright
# ---------------------------------------------------------------------------

@dataclass
class RateWindow:
    used_percent: float
    resets_at: Optional[datetime] = None
    reset_description: Optional[str] = None


@dataclass
class OpenAIUsage:
    credits_remaining: Optional[float] = None
    code_review_remaining_percent: Optional[float] = None
    primary_limit: Optional[RateWindow] = None    # 5-hour
    secondary_limit: Optional[RateWindow] = None  # weekly
    account_plan: Optional[str] = None
    raw_text: str = ""


def _parse_reset_description(text: str) -> Optional[datetime]:
    """Try to parse a reset time from various formats:
    - 'Resets Apr 23, 2026 10:08 AM'
    - 'resets in 2h 30m'
    """
    from datetime import timedelta
    # Absolute date: "Resets Apr 23, 2026 10:08 AM"
    m = re.search(
        r"resets?\s+([A-Za-z]{3}\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM))",
        text, re.I,
    )
    if m:
        try:
            return datetime.strptime(m.group(1).strip(), "%b %d, %Y %I:%M %p").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # Relative: "resets in 2h 30m"
    m = re.search(r"resets?\s+in\s+(\d+)\s*h(?:our)?s?(?:\s+(\d+)\s*m)?", text, re.I)
    if m:
        hours = int(m.group(1))
        minutes = int(m.group(2) or 0)
        return datetime.now(timezone.utc) + timedelta(hours=hours, minutes=minutes)
    return None


def _parse_rate_window(lines: list[str], keywords: list[str]) -> Optional[RateWindow]:
    """Find a rate limit window by scanning lines for keywords.

    Searches only in lines *after* the keyword match to avoid picking up
    values from a preceding section.
    """
    for i, line in enumerate(lines):
        if any(kw in line.lower() for kw in keywords):
            # Only look forward from the keyword line; include a few lines
            # for the reset date which may follow on a separate line
            forward = " ".join(lines[i:i + 8])
            m = re.search(r"(\d{1,3})\s*%\s*(?:remaining|left)", forward, re.I)
            if m:
                remaining_pct = float(m.group(1))
                used_pct = 100.0 - remaining_pct
                resets_at = _parse_reset_description(forward)
                return RateWindow(
                    used_percent=used_pct,
                    resets_at=resets_at,
                    reset_description=forward.strip()[:120],
                )
    return None


def _parse_openai_body_text(body_text: str) -> OpenAIUsage:
    """Parse usage data from the page's body text (mirrors codexbar's regex approach)."""
    usage = OpenAIUsage(raw_text=body_text)
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]

    # Credits remaining
    m = re.search(r"([\d,]+(?:\.\d+)?)\s+credits?\s+remaining", body_text, re.I)
    if m:
        usage.credits_remaining = float(m.group(1).replace(",", ""))

    # Code review remaining %
    m = re.search(r"code\s*review[^0-9%]*(\d{1,3})\s*%\s*remaining", body_text, re.I)
    if m:
        usage.code_review_remaining_percent = float(m.group(1))

    # 5-hour rate limit
    usage.primary_limit = _parse_rate_window(lines, ["5h", "5-hour", "5 hour"])

    # Weekly rate limit
    usage.secondary_limit = _parse_rate_window(lines, ["weekly", "7-day", "7 day", "7d"])

    # Account plan from text
    m = re.search(r"\b(free|plus|pro|team|enterprise)\b", body_text, re.I)
    if m:
        usage.account_plan = m.group(1).lower()

    return usage


def _read_cookies_from_file() -> dict[str, str]:
    """Load saved chatgpt.com cookies from config file."""
    if not OPENAI_COOKIES_PATH.exists():
        return {}
    with open(OPENAI_COOKIES_PATH) as f:
        return json.load(f)


def _read_chrome_cookies(domain: str) -> dict[str, str]:
    """Read cookies for a domain from Chrome's cookie store."""
    try:
        import browser_cookie3  # type: ignore
        jar = browser_cookie3.chrome(domain_name=domain)
        cookies = {c.name: c.value for c in jar}
        if cookies:
            return cookies
    except Exception:
        pass
    try:
        import rookiepy  # type: ignore
        cookies = rookiepy.chrome([domain])
        return {c["name"]: c["value"] for c in cookies}
    except Exception:
        pass
    return {}


async def _scrape_openai_usage(cookies: dict[str, str]) -> OpenAIUsage:
    from playwright.async_api import async_playwright  # type: ignore

    pw_cookies = []
    for k, v in cookies.items():
        if k.startswith("__Host-"):
            # __Host- cookies must have no domain and path="/"
            pw_cookies.append({"name": k, "value": v, "domain": "chatgpt.com", "path": "/", "secure": True})
        else:
            pw_cookies.append({"name": k, "value": v, "domain": ".chatgpt.com", "path": "/", "secure": True})

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        await context.add_cookies(pw_cookies)

        page = await context.new_page()
        await page.goto(OPENAI_USAGE_URL, wait_until="networkidle", timeout=20000)

        body_text = await page.inner_text("body")
        await browser.close()

    return _parse_openai_body_text(body_text)


def get_openai_usage(cookies: Optional[dict[str, str]] = None) -> OpenAIUsage:
    """Fetch OpenAI Codex usage by scraping the settings page.

    Cookie resolution order:
      1. cookies= argument (explicit)
      2. ~/.config/token-usage-dash/openai-cookies.json
      3. Chrome browser cookie store (if installed)
    """
    if cookies is None:
        cookies = _read_cookies_from_file()
    if not cookies:
        cookies = _read_chrome_cookies("chatgpt.com")
    if not cookies:
        raise RuntimeError(
            "No chatgpt.com cookies found.\n"
            f"Save cookies to {OPENAI_COOKIES_PATH} as JSON, "
            "or install browser_cookie3 with Chrome logged into chatgpt.com.\n"
            "See file header for instructions."
        )
    return asyncio.run(_scrape_openai_usage(cookies))


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_time_until(dt: Optional[datetime]) -> str:
    if dt is None:
        return "unknown"
    delta = dt - datetime.now(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "now"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h {m}m" if h > 0 else f"{m}m"


def format_time_until_iso(iso_str: str) -> str:
    return format_time_until(datetime.fromisoformat(iso_str))


def _bar(used_pct: float, width: int = 20) -> str:
    filled = int(used_pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def print_claude_usage(usage: dict) -> None:
    labels = {
        "five_hour":        "5-hour   ",
        "seven_day":        "7-day    ",
        "seven_day_sonnet": "7d Sonnet",
        "seven_day_opus":   "7d Opus  ",
    }
    print("Claude plan usage:")
    any_data = False
    for key, label in labels.items():
        window = usage.get(key)
        if not window:
            continue
        any_data = True
        util = window["utilization"]
        remaining = 100 - util
        resets = format_time_until_iso(window["resets_at"])
        print(f"  {label}  [{_bar(util)}] {util:5.1f}% used  {remaining:5.1f}% left  resets in {resets}")
    if not any_data:
        print("  No usage data returned.")


def print_openai_usage(usage: OpenAIUsage) -> None:
    print("OpenAI Codex plan usage:")
    if usage.account_plan:
        print(f"  Plan: {usage.account_plan}")
    if usage.credits_remaining is not None:
        print(f"  Credits remaining: {usage.credits_remaining:,.1f}")
    if usage.code_review_remaining_percent is not None:
        used = 100 - usage.code_review_remaining_percent
        print(f"  Code review  [{_bar(used)}] {used:.1f}% used  {usage.code_review_remaining_percent:.1f}% left")
    if usage.primary_limit:
        w = usage.primary_limit
        resets = format_time_until(w.resets_at)
        print(f"  5-hour       [{_bar(w.used_percent)}] {w.used_percent:.1f}% used  {100-w.used_percent:.1f}% left  resets in {resets}")
    if usage.secondary_limit:
        w = usage.secondary_limit
        resets = format_time_until(w.resets_at)
        print(f"  Weekly       [{_bar(w.used_percent)}] {w.used_percent:.1f}% used  {100-w.used_percent:.1f}% left  resets in {resets}")
    if not any([usage.credits_remaining, usage.primary_limit, usage.secondary_limit,
                usage.code_review_remaining_percent]):
        print("  No usage data found — may need to log in or page structure changed.")
        if usage.raw_text:
            print(f"  Page preview: {usage.raw_text[:200]!r}")


def cmd_save_cookies(cookie_header: str) -> None:
    """Parse a raw Cookie: header string and save to config file."""
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    OPENAI_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OPENAI_COOKIES_PATH, "w") as f:
        json.dump(cookies, f, indent=2)
    print(f"Saved {len(cookies)} cookies to {OPENAI_COOKIES_PATH}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Show subscription plan usage")
    parser.add_argument("--claude-only", action="store_true")
    parser.add_argument("--openai-only", action="store_true")
    parser.add_argument(
        "--save-openai-cookies",
        metavar="COOKIE_HEADER",
        help="Parse and save a raw Cookie: header string for chatgpt.com",
    )
    args = parser.parse_args()

    if args.save_openai_cookies:
        cmd_save_cookies(args.save_openai_cookies)
        return

    show_claude = not args.openai_only
    show_openai = not args.claude_only
    errors = []

    if show_claude:
        print()
        try:
            print_claude_usage(get_claude_usage())
        except Exception as e:
            errors.append(str(e))
            print(f"Claude: error — {e}")

    if show_openai:
        print()
        try:
            print_openai_usage(get_openai_usage())
        except Exception as e:
            errors.append(str(e))
            print(f"OpenAI: error — {e}")

    print()
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
