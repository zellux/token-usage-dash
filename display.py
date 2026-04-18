#!/usr/bin/env python3
"""
Update e-ink display (dot.mindreset.tech) with Claude + OpenAI Codex usage.
Renders a 296×152 PNG image and pushes via the Image API.

Required .env keys:
  QUOTE_API_KEY     - Bearer token for dot.mindreset.tech
  QUOTE_DEVICE_ID   - Device serial number (also accepts DEVICE_ID)

Optional .env keys:
  OPENAI_ENABLED=false   - Set to false to skip OpenAI scrape (default: true)
  UPDATE_INTERVAL=1800   - Seconds between updates when running with --loop (default: 1800)
"""

import base64
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

API_BASE = "https://dot.mindreset.tech"
API_KEY = os.environ.get("QUOTE_API_KEY", "")
DEVICE_ID = os.environ.get("QUOTE_DEVICE_ID", "") or os.environ.get("DEVICE_ID", "")
OPENAI_ENABLED = os.environ.get("OPENAI_ENABLED", "true").lower() != "false"
UPDATE_INTERVAL = int(os.environ.get("UPDATE_INTERVAL", "1800"))


# ---------------------------------------------------------------------------
# Shared helpers (used by render.py too)
# ---------------------------------------------------------------------------

def format_time_until(dt) -> str:
    if dt is None:
        return "?"
    delta = dt - datetime.now(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "now"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if h > 0 else f"{m}m"


def format_time_until_iso(iso_str: str) -> str:
    from datetime import datetime
    return format_time_until(datetime.fromisoformat(iso_str))


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def _check_config() -> None:
    if not API_KEY:
        raise RuntimeError("QUOTE_API_KEY not set in .env")
    if not DEVICE_ID:
        raise RuntimeError("QUOTE_DEVICE_ID not set in .env")


def push_image(png_bytes: bytes) -> None:
    _check_config()
    url = f"{API_BASE}/api/authV2/open/device/{DEVICE_ID}/image"
    payload = {
        "refreshNow": True,
        "image": base64.b64encode(png_bytes).decode(),
        "ditherType": "NONE",
    }
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=20,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once(save_preview: bool = False) -> bool:
    """Fetch usage, render image, push to display. Returns True on success."""
    from usage import get_claude_usage, get_openai_usage
    from render import render_image

    claude_usage = None
    openai_usage = None

    try:
        claude_usage = get_claude_usage()
    except Exception as e:
        print(f"Warning: Claude fetch failed — {e}", file=sys.stderr)

    if OPENAI_ENABLED:
        try:
            openai_usage = get_openai_usage()
        except Exception as e:
            print(f"Warning: OpenAI fetch failed — {e}", file=sys.stderr)

    if claude_usage is None and openai_usage is None:
        print("Error: no usage data, skipping update.", file=sys.stderr)
        return False

    png = render_image(claude_usage, openai_usage)

    if save_preview:
        preview_path = "/tmp/usage_preview.png"
        with open(preview_path, "wb") as f:
            f.write(png)
        print(f"Preview saved to {preview_path}")

    try:
        push_image(png)
        now = datetime.now().strftime("%H:%M:%S")
        print(f"[{now}] Display updated ({len(png):,} bytes)")
        return True
    except Exception as e:
        print(f"Error: display push failed — {e}", file=sys.stderr)
        return False


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Push token usage image to e-ink display")
    parser.add_argument("--loop", action="store_true",
                        help=f"Run repeatedly every UPDATE_INTERVAL seconds (default: {UPDATE_INTERVAL}s)")
    parser.add_argument("--interval", type=int, default=UPDATE_INTERVAL,
                        help="Seconds between updates")
    parser.add_argument("--preview", action="store_true",
                        help="Also save PNG to /tmp/usage_preview.png")
    args = parser.parse_args()

    if args.loop:
        print(f"Looping every {args.interval}s. Ctrl+C to stop.")
        while True:
            run_once(save_preview=args.preview)
            time.sleep(args.interval)
    else:
        success = run_once(save_preview=args.preview)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
