from __future__ import annotations

import html
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
from .models import FrameConfig, FrameType, PANEL_WIDTH, PANEL_HEIGHT

PACKAGE_DIR = Path(__file__).parent
FONT_DIRS = [PACKAGE_DIR / "fonts", PACKAGE_DIR.parent / "fonts"]
DIGIT_DIRS = [PACKAGE_DIR / "digits", PACKAGE_DIR.parent / "digits"]
CLOCK_CHARACTER_SPACING = 2


def discover_fonts() -> dict[str, Path]:
    fonts: dict[str, Path] = {}
    for font_dir in FONT_DIRS:
        if not font_dir.exists():
            continue
        for path in sorted(font_dir.glob("*.ttf")):
            fonts[path.stem.replace("-", " ").replace("_", " ")] = path
    # Keep the original friendly name if the requested file is present.
    for path in FONT_DIRS:
        vcr = path / "VCR-OSD-Mono.ttf"
        if vcr.exists():
            fonts["VCR OSD Mono"] = vcr
    return fonts


FONT_ALIASES = discover_fonts() or {"Default": Path()}
_SCROLLING = {"Right to left", "Left to right", "Top to bottom", "Bottom to top"}
_feed_cache: dict[str, tuple[float, list[str]]] = {}
_live_cache: dict[str, tuple[float, str]] = {}


def load_font(name: str, size: int) -> ImageFont.ImageFont:
    path = FONT_ALIASES.get(name)
    if path and path.exists():
        return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _digit_asset_path(name: str) -> Path | None:
    for digit_dir in DIGIT_DIRS:
        path = digit_dir / name
        if path.exists():
            return path
    return None


def _clock_bitmap(name: str, foreground: tuple[int, int, int], background: tuple[int, int, int]) -> Image.Image | None:
    path = _digit_asset_path(name)
    if path is None:
        return None
    src = Image.open(path).convert("RGBA")
    alpha = src.getchannel("A")
    luminance = src.convert("L")
    mask = Image.new("L", src.size, 0)
    for y in range(src.height):
        for x in range(src.width):
            visible = alpha.getpixel((x, y)) > 0 and luminance.getpixel((x, y)) >= 128
            mask.putpixel((x, y), 255 if visible else 0)
    out = Image.new("RGB", src.size, background)
    out.paste(Image.new("RGB", src.size, foreground), mask=mask)
    return out


def quantize_panel(img: Image.Image) -> Image.Image:
    return img.convert("RGB").quantize(colors=256, dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")


def _text_bbox(font: ImageFont.ImageFont, text: str) -> tuple[int, int]:
    if not text:
        return 0, 0
    box = font.getbbox(text)
    return box[2] - box[0], box[3] - box[1]


def _draw_crisp_text(base: Image.Image, xy: tuple[int, int], text: str, font: ImageFont.ImageFont, fill: tuple[int, int, int]) -> None:
    if not text:
        return
    mask = Image.new("L", base.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255)
    mask = mask.point(lambda p: 255 if p >= 128 else 0)
    color = Image.new("RGB", base.size, fill)
    base.paste(color, mask=mask)


def _load_icon(path: str | None) -> Image.Image | None:
    if not path:
        return None
    try:
        icon = Image.open(path).convert("RGBA")
    except Exception:
        return None
    return ImageOps.contain(icon, (16, 16), Image.Resampling.LANCZOS)


def _paste_icon(canvas: Image.Image, icon: Image.Image | None, y: int = 0) -> int:
    if icon is None:
        return 0
    canvas.paste(icon.convert("RGB"), (0, max(0, y + (PANEL_HEIGHT - icon.height) // 2)), icon)
    return 18


def _wrap_text(text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    words = text.split() or [text]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and _text_bbox(font, candidate)[0] > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def render_text_block(settings: dict, text: str, tick: int = 0, *, force_scroll: bool = False) -> Image.Image:
    fg = tuple(settings.get("foreground", (255, 255, 0)))
    bg = tuple(settings.get("background", (0, 0, 0)))
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg)
    icon_x = _paste_icon(img, _load_icon(settings.get("icon_path")))
    font = load_font(settings.get("font", "VCR OSD Mono"), int(settings.get("font_size", 16)))
    spacing = int(settings.get("horizontal_spacing", 0))
    yoff = int(settings.get("vertical_offset", 0))
    available = PANEL_WIDTH - icon_x
    scrolling = settings.get("scrolling", "None (Wrap)")
    if force_scroll:
        scrolling = "Right to left"
    w, h = _text_bbox(font, text)
    if scrolling in _SCROLLING:
        speed = max(1, int(settings.get("scroll_speed", 4)))
        if scrolling == "Right to left":
            x = icon_x + available - ((tick * speed) % max(1, available + w + spacing))
            y = (PANEL_HEIGHT - h) // 2 + yoff
        elif scrolling == "Left to right":
            x = icon_x - w + ((tick * speed) % max(1, available + w + spacing))
            y = (PANEL_HEIGHT - h) // 2 + yoff
        elif scrolling == "Top to bottom":
            x = icon_x + max(0, (available - w) // 2)
            y = -h + ((tick * speed) % max(1, PANEL_HEIGHT + h)) + yoff
        else:
            x = icon_x + max(0, (available - w) // 2)
            y = PANEL_HEIGHT - ((tick * speed) % max(1, PANEL_HEIGHT + h)) + yoff
        _draw_crisp_text(img, (int(x), int(y)), text, font, fg)
        return quantize_panel(img)

    lines = _wrap_text(text, font, available)
    line_h = max(1, _text_bbox(font, "Ag")[1] + 1)
    total_h = min(len(lines), max(1, PANEL_HEIGHT // line_h + 1)) * line_h
    y = (PANEL_HEIGHT - total_h) // 2 + yoff
    for line in lines:
        line_w, _ = _text_bbox(font, line)
        x = icon_x + max(0, (available - line_w) // 2)
        _draw_crisp_text(img, (x, y), line, font, fg)
        y += line_h
        if y >= PANEL_HEIGHT:
            break
    return quantize_panel(img)


def fit_image(path: str | None, mode: str) -> Image.Image:
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), (0, 0, 0))
    if not path:
        return img
    try:
        src = Image.open(path).convert("RGB")
    except Exception:
        return img
    if mode == "Stretch to fit":
        fitted = src.resize((PANEL_WIDTH, PANEL_HEIGHT), Image.Resampling.LANCZOS)
    elif mode == "Crop to fit":
        fitted = ImageOps.fit(src, (PANEL_WIDTH, PANEL_HEIGHT), Image.Resampling.LANCZOS)
    else:
        fitted = ImageOps.contain(src, (PANEL_WIDTH, PANEL_HEIGHT), Image.Resampling.LANCZOS)
        img.paste(fitted, ((PANEL_WIDTH - fitted.width) // 2, (PANEL_HEIGHT - fitted.height) // 2))
        return quantize_panel(img)
    return quantize_panel(fitted)

SEGMENTS = {
    "0": "abcefd", "1": "bc", "2": "abged", "3": "abgcd", "4": "fbgc", "5": "afgcd",
    "6": "afgecd", "7": "abc", "8": "abcdefg", "9": "abfgcd",
}


def draw_digit(draw: ImageDraw.ImageDraw, x: int, digit: str, color: tuple[int, int, int]):
    segs = SEGMENTS.get(digit, "")
    y = 0; w = 8; h = 15; t = 3
    rects = {
        "a": (x + t, y, x + w - t, y + t - 1), "b": (x + w - t, y + t, x + w - 1, y + h // 2),
        "c": (x + w - t, y + h // 2, x + w - 1, y + h - t), "d": (x + t, y + h - t, x + w - t, y + h - 1),
        "e": (x, y + h // 2, x + t - 1, y + h - t), "f": (x, y + t, x + t - 1, y + h // 2),
        "g": (x + t, y + h // 2 - 1, x + w - t, y + h // 2 + 1),
    }
    for s in segs:
        draw.rectangle(rects[s], fill=color)


def _render_bitmap_clock(settings: dict, tick: int = 0) -> Image.Image | None:
    bg = tuple(settings.get("background", (0, 0, 0)))
    fg = tuple(settings.get("foreground", (255, 255, 0)))
    now = time.localtime()
    hour = now.tm_hour
    suffix = None
    if settings.get("time_mode") == "12-hour":
        suffix = "am.png" if hour < 12 else "pm.png"
        hour = hour % 12 or 12
    parts = list(f"{hour}:{now.tm_min:02d}")
    if settings.get("show_seconds"):
        parts.extend(list(f":{now.tm_sec:02d}"))
    bitmaps: list[Image.Image] = []
    for ch in parts:
        if ch == ":" and settings.get("flash_separator") and tick % 2:
            sep = _clock_bitmap("separator.png", bg, bg)
        elif ch == ":":
            sep = _clock_bitmap("separator.png", fg, bg)
        else:
            sep = _clock_bitmap(f"digit-{ch}.png", fg, bg)
        if sep is None:
            return None
        bitmaps.append(sep)
    if suffix:
        suffix_img = _clock_bitmap(suffix, fg, bg)
        if suffix_img is None:
            return None
        bitmaps.append(suffix_img)
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg)
    icon_x = _paste_icon(img, _load_icon(settings.get("icon_path")))
    total_w = sum(part.width for part in bitmaps) + CLOCK_CHARACTER_SPACING * max(0, len(bitmaps) - 1)
    x = icon_x + max(0, (PANEL_WIDTH - icon_x - total_w) // 2)
    for part in bitmaps:
        y = max(0, (PANEL_HEIGHT - part.height) // 2)
        img.paste(part, (x, y))
        x += part.width + CLOCK_CHARACTER_SPACING
    return quantize_panel(img)


def render_clock(settings: dict, tick: int = 0) -> Image.Image:
    bitmap = _render_bitmap_clock(settings, tick)
    if bitmap is not None:
        return bitmap
    bg = tuple(settings.get("background", (0, 0, 0)))
    fg = tuple(settings.get("foreground", (255, 255, 0)))
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg)
    icon_x = _paste_icon(img, _load_icon(settings.get("icon_path")))
    fmt = "%l:%M" if settings.get("time_mode") == "12-hour" else "%H:%M"
    if settings.get("show_seconds"):
        fmt += ":%S"
    text = time.strftime(fmt)
    if settings.get("flash_separator") and tick % 2:
        text = text.replace(":", " ")
    draw = ImageDraw.Draw(img)
    digit_w = 9
    width = sum(4 if c == ":" else digit_w for c in text)
    x = icon_x + max(0, (PANEL_WIDTH - icon_x - width) // 2)
    for ch in text:
        if ch.isdigit():
            draw_digit(draw, x, ch, fg); x += digit_w
        else:
            if ch == ":":
                draw.rectangle((x + 1, 4, x + 3, 6), fill=fg); draw.rectangle((x + 1, 10, x + 3, 12), fill=fg)
            x += 4
    return quantize_panel(img)


def _clean_title(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[^\w\s.,:;!?£$€%&()\-'/]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()[:256]


def fetch_feed_titles(url: str, count: int) -> list[str]:
    if not url:
        return ["RSS feed URL not set"]
    cached = _feed_cache.get(url)
    if cached and time.time() - cached[0] < 600:
        return cached[1][:count]
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            data = response.read(512_000)
        root = ET.fromstring(data)
        titles = [_clean_title(el.text or "") for el in root.findall(".//item/title")]
        if not titles:
            titles = [_clean_title(el.text or "") for el in root.findall(".//{http://www.w3.org/2005/Atom}entry/{http://www.w3.org/2005/Atom}title")]
        titles = [t for t in titles if t][:count] or ["No RSS titles found"]
    except Exception:
        titles = ["RSS feed unavailable"]
    _feed_cache[url] = (time.time(), titles)
    return titles


def fetch_live_text(url: str) -> str:
    if not url:
        return ""
    cached = _live_cache.get(url)
    if cached and time.time() - cached[0] < 30:
        return cached[1]
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            text = response.read(4096).decode("utf-8", errors="replace")
        text = _clean_title(text)
    except Exception:
        text = "unavailable"
    _live_cache[url] = (time.time(), text)
    return text


def render_frame(frame: FrameConfig, tick: int = 0) -> Image.Image:
    settings = frame.merged_settings()
    if frame.frame_type is FrameType.TEXT:
        return render_text_block(settings, settings.get("message", ""), tick)
    if frame.frame_type is FrameType.IMAGE:
        return fit_image(settings.get("path"), settings.get("display", "Resize to fit"))
    if frame.frame_type is FrameType.CLOCK:
        return render_clock(settings, tick)
    if frame.frame_type is FrameType.DATE:
        return render_text_block(settings, time.strftime(settings.get("date_format", "%d/%m/%Y")), tick)
    if frame.frame_type is FrameType.RSS:
        titles = fetch_feed_titles(settings.get("feed_url", ""), int(settings.get("item_count", 5)))
        return render_text_block(settings, titles[tick % max(1, len(titles))], tick, force_scroll=True)
    if frame.frame_type is FrameType.LIVE_TEXT:
        text = f"{settings.get('label', '')}: {fetch_live_text(settings.get('rest_url', ''))}".strip()
        return render_text_block(settings, text, tick)
    return Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), (0, 0, 0))
