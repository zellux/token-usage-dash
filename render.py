"""
Render a 296×152 black/white PNG image showing Claude + OpenAI Codex usage.
Sections are stacked vertically, one row per metric.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

W, H = 296, 152
PAD = 5

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

BLACK = 0
WHITE = 255

LABEL_W = 36   # fixed width for the row label column
BAR_H   = 9    # progress bar height
ROW_GAP = 3    # vertical gap between rows


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _text_w(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    return int(draw.textlength(text, font=font))


def _bar(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, used_pct: float) -> None:
    draw.rectangle([x, y, x + w - 1, y + h - 1], outline=BLACK, width=1)
    filled = int((w - 2) * min(used_pct, 100) / 100)
    if filled > 0:
        draw.rectangle([x + 1, y + 1, x + filled, y + h - 2], fill=BLACK)


def _row(
    draw: ImageDraw.ImageDraw,
    y: int,
    label: str,
    used_pct: float,
    note: Optional[str],
    fonts: dict,
) -> int:
    """Draw one metric row. Returns the y position after this row."""
    remaining = 100.0 - used_pct

    # Label (left-aligned, fixed width)
    draw.text((PAD, y), label, font=fonts["small_bold"], fill=BLACK)

    # Note (right-aligned: "87% · 3h26m")
    note_text = f"{remaining:.0f}%"
    if note:
        note_text += f" {note}"
    note_w = _text_w(draw, note_text, fonts["tiny"])
    draw.text((W - PAD - note_w, y), note_text, font=fonts["tiny"], fill=BLACK)

    # Bar between label and note
    bar_x = PAD + LABEL_W
    bar_w = W - PAD - note_w - PAD - bar_x - 4
    draw.text  # already called above
    _bar(draw, bar_x, y, bar_w, BAR_H, used_pct)

    return y + BAR_H + ROW_GAP


def _section_header(draw: ImageDraw.ImageDraw, y: int, label: str, fonts: dict) -> int:
    draw.text((PAD, y), label, font=fonts["bold"], fill=BLACK)
    return y + fonts["bold"].size + 2


def render_image(
    claude_usage: Optional[dict],
    openai_usage=None,
) -> bytes:
    img = Image.new("L", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    fonts = {
        "title":      _font(FONT_BOLD,    11),
        "bold":       _font(FONT_BOLD,     9),
        "small_bold": _font(FONT_BOLD,     8),
        "tiny":       _font(FONT_REGULAR,  8),
    }

    # ── Header ────────────────────────────────────────────────────────────
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    date_str = now.strftime("%b %-d")
    time_str = now.strftime("%-I:%M %p")
    draw.text((PAD, PAD), "Token Usage", font=fonts["title"], fill=BLACK)
    ts_w = _text_w(draw, time_str, fonts["tiny"])
    dt_w = _text_w(draw, date_str, fonts["tiny"])
    draw.text((W - PAD - ts_w, PAD + 2), time_str, font=fonts["tiny"], fill=BLACK)
    draw.text((W - PAD - ts_w - 4 - dt_w, PAD + 2), date_str, font=fonts["tiny"], fill=BLACK)

    header_bottom = PAD + fonts["title"].size + 3
    draw.line([(0, header_bottom), (W, header_bottom)], fill=BLACK, width=1)

    y = header_bottom + 4

    # ── Claude section ────────────────────────────────────────────────────
    if claude_usage:
        from display import format_time_until_iso

        def _note(window: dict) -> Optional[str]:
            try:
                return format_time_until_iso(window["resets_at"])
            except Exception:
                return None

        rows = [
            (key, lbl)
            for key, lbl in [
                ("five_hour",        "5h"),
                ("seven_day",        "7d"),
                ("seven_day_sonnet", "7dS"),
                ("seven_day_opus",   "7dO"),
            ]
            if claude_usage.get(key)
        ]

        if rows:
            y = _section_header(draw, y, "Claude", fonts)
            for key, lbl in rows:
                w = claude_usage[key]
                y = _row(draw, y, lbl, w["utilization"], _note(w), fonts)

    # ── Dashed divider ────────────────────────────────────────────────────
    if claude_usage and openai_usage:
        dash, gap = 6, 4
        x = 0
        while x < W:
            draw.line([(x, y), (min(x + dash - 1, W), y)], fill=BLACK, width=1)
            x += dash + gap
        y += 4

    # ── OpenAI section ────────────────────────────────────────────────────
    if openai_usage:
        from display import format_time_until

        label = "OpenAI Codex"
        if openai_usage.credits_remaining is not None:
            label += f"  ({openai_usage.credits_remaining:.0f} cr)"
        y = _section_header(draw, y, label, fonts)

        if openai_usage.primary_limit:
            w = openai_usage.primary_limit
            note = format_time_until(w.resets_at) if w.resets_at else None
            y = _row(draw, y, "5h", w.used_percent, note, fonts)
        if openai_usage.secondary_limit:
            w = openai_usage.secondary_limit
            note = format_time_until(w.resets_at) if w.resets_at else None
            y = _row(draw, y, "Wk", w.used_percent, note, fonts)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    from usage import get_claude_usage, get_openai_usage

    claude = None
    openai = None
    try:
        claude = get_claude_usage()
    except Exception as e:
        print(f"Claude error: {e}")
    try:
        openai = get_openai_usage()
    except Exception as e:
        print(f"OpenAI error: {e}")

    png = render_image(claude, openai)
    with open("/tmp/usage_preview.png", "wb") as f:
        f.write(png)
    print(f"Saved preview to /tmp/usage_preview.png ({len(png)} bytes)")
