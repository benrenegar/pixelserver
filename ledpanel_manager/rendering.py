from __future__ import annotations

from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
from .models import PANEL_HEIGHT, PANEL_WIDTH, FrameConfig, FrameType

FONT_DIR = Path(__file__).with_name("fonts")
FONT_ALIASES = {"VCR OSD Mono": FONT_DIR / "VCR-OSD-Mono.ttf"}
SEGMENTS = {
    "0": "abcedf", "1": "cf", "2": "acdeg", "3": "acdfg", "4": "bcfg",
    "5": "abdfg", "6": "abdefg", "7": "acf", "8": "abcdefg", "9": "abcdfg",
}


def load_font(name: str, size: int) -> ImageFont.ImageFont:
    path = FONT_ALIASES.get(name)
    try:
        if path and path.exists():
            return ImageFont.truetype(str(path), size=size)
        return ImageFont.truetype(name, size=size)
    except Exception:
        return ImageFont.load_default()


def quantize_panel(img: Image.Image) -> Image.Image:
    return img.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")


def fit_image(path: str | Path | None, mode: str) -> Image.Image:
    canvas = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), (0, 0, 0))
    if not path:
        return canvas
    src = Image.open(path).convert("RGB")
    if mode == "Stretch to fit":
        return quantize_panel(src.resize((PANEL_WIDTH, PANEL_HEIGHT), Image.Resampling.LANCZOS))
    if mode == "Crop to fit":
        src = ImageOps.fit(src, (PANEL_WIDTH, PANEL_HEIGHT), method=Image.Resampling.LANCZOS)
        return quantize_panel(src)
    src.thumbnail((PANEL_WIDTH, PANEL_HEIGHT), Image.Resampling.LANCZOS)
    canvas.paste(src, ((PANEL_WIDTH - src.width) // 2, (PANEL_HEIGHT - src.height) // 2))
    return quantize_panel(canvas)


def _draw_text_no_antialias(mask: Image.Image, xy: tuple[int, int], text: str, font: ImageFont.ImageFont, h_spacing: int, v_spacing: int) -> None:
    draw = ImageDraw.Draw(mask)
    x0, y = xy
    line_height = max(1, font.getbbox("Mg")[3] - font.getbbox("Mg")[1]) + v_spacing
    for line in text.splitlines() or [""]:
        x = x0
        for ch in line:
            draw.text((x, y), ch, fill=255, font=font)
            bbox = draw.textbbox((0, 0), ch, font=font)
            x += max(1, bbox[2] - bbox[0]) + h_spacing
        y += line_height


def _text_bbox(text: str, font: ImageFont.ImageFont, h_spacing: int, v_spacing: int) -> tuple[int, int]:
    probe = Image.new("L", (1, 1)); draw = ImageDraw.Draw(probe)
    widths = []
    for line in text.splitlines() or [""]:
        width = 0
        for ch in line:
            bbox = draw.textbbox((0, 0), ch, font=font)
            width += max(1, bbox[2] - bbox[0]) + h_spacing
        widths.append(max(0, width - h_spacing))
    bbox = font.getbbox("Mg")
    line_height = max(1, bbox[3] - bbox[1]) + v_spacing
    return max(widths or [0]), max(1, line_height * max(1, len(widths)) - v_spacing)


def render_text(settings: dict, offset: int = 0) -> Image.Image:
    fg, bg = settings["foreground"], settings["background"]
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg)
    font = load_font(settings.get("font", "VCR OSD Mono"), int(settings.get("font_size", 16)))
    text = settings.get("message", "")
    h_spacing = int(settings.get("horizontal_spacing", 0))
    v_spacing = int(settings.get("vertical_spacing", 0))
    tw, th = _text_bbox(text, font, h_spacing, v_spacing)
    x, y = 0, (PANEL_HEIGHT - th) // 2
    scrolling = settings.get("scrolling", "None")
    if scrolling == "Right to left": x = PANEL_WIDTH - offset
    elif scrolling == "Left to right": x = offset - tw
    elif scrolling == "Top to bottom": y = offset - th
    elif scrolling == "Bottom to top": y = PANEL_HEIGHT - offset
    mask = Image.new("L", (PANEL_WIDTH, PANEL_HEIGHT), 0)
    _draw_text_no_antialias(mask, (x, y), text, font, h_spacing, v_spacing)
    mask = mask.point(lambda p: 255 if p >= 128 else 0)
    fg_img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), fg)
    img.paste(fg_img, mask=mask)
    return quantize_panel(img)


def render_clock(settings: dict, now: datetime | None = None) -> Image.Image:
    now = now or datetime.now()
    fmt = "%H:%M:%S" if settings.get("show_seconds") else "%H:%M"
    if settings.get("time_mode") == "12-hour": fmt = "%I:%M:%S" if settings.get("show_seconds") else "%I:%M"
    text = now.strftime(fmt).lstrip("0") or "0"
    if settings.get("flash_separator") and now.second % 2: text = text.replace(":", " ")
    return render_seven_segment_text(text, settings["foreground"], settings["background"])


def render_seven_segment_text(text: str, fg, bg) -> Image.Image:
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg)
    draw = ImageDraw.Draw(img)
    digit_w, colon_w, gap = 8, 3, 1
    widths = [colon_w if ch == ":" else digit_w for ch in text]
    total_w = sum(widths) + gap * max(0, len(widths) - 1)
    x = max(0, (PANEL_WIDTH - total_w) // 2)
    y = 0
    for ch, width in zip(text, widths):
        if ch == ":":
            draw.rectangle((x, y + 4, x + 2, y + 6), fill=fg)
            draw.rectangle((x, y + 10, x + 2, y + 12), fill=fg)
        elif ch in SEGMENTS:
            _draw_digit(draw, x, y, SEGMENTS[ch], fg)
        x += width + gap
    return img


def _draw_digit(draw: ImageDraw.ImageDraw, x: int, y: int, segments: str, fg) -> None:
    # 8x15 digit using 3-pixel strokes.
    rects = {
        "a": (x + 1, y, x + 6, y + 2),
        "b": (x, y + 1, x + 2, y + 6),
        "c": (x + 5, y + 1, x + 7, y + 6),
        "d": (x + 1, y + 12, x + 6, y + 14),
        "e": (x, y + 8, x + 2, y + 13),
        "f": (x + 5, y + 8, x + 7, y + 13),
        "g": (x + 1, y + 6, x + 6, y + 8),
    }
    for seg in segments:
        draw.rectangle(rects[seg], fill=fg)


def render_date(settings: dict, now: datetime | None = None) -> Image.Image:
    settings = settings | {"message": (now or datetime.now()).strftime(settings.get("date_format", "%d/%m/%Y")), "scrolling": "None"}
    return render_text(settings)


def render_frame(frame: FrameConfig, tick: int = 0) -> Image.Image:
    s = frame.merged_settings()
    if frame.frame_type is FrameType.IMAGE: return fit_image(s.get("path"), s.get("display", "Resize to fit"))
    if frame.frame_type is FrameType.CLOCK: return render_clock(s)
    if frame.frame_type is FrameType.DATE: return render_date(s)
    return render_text(s, tick * max(1, int(s.get("scroll_speed", 4))))
