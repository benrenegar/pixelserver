from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
from .models import DEFAULT_FONT, DEFAULT_FONT_SIZE, DEFAULT_FOREGROUND, FrameConfig, FrameType, PANEL_WIDTH, PANEL_HEIGHT

PACKAGE_DIR = Path(__file__).parent
FONT_DIRS = [PACKAGE_DIR / "fonts", PACKAGE_DIR.parent / "fonts"]
DIGIT_DIRS = [PACKAGE_DIR / "digits", PACKAGE_DIR.parent / "digits"]
DEFAULT_CLOCK_CHARACTER_SPACING = 2


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
_weather_cache: dict[tuple[str, str], tuple[float, str | None, float | None, str | None]] = {}
_SCROLLING = {"Right to left", "Left to right", "Top to bottom", "Bottom to top"}


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


def _clock_bitmap(name: str, foreground: tuple[int, int, int], background: tuple[int, int, int], overrides: dict | None = None) -> Image.Image | None:
    override_path = (overrides or {}).get(name)
    path = Path(override_path) if override_path else _digit_asset_path(name)
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

def _icon_and_text_layer(
    settings: dict,
    text: str,
    font: ImageFont.ImageFont,
    foreground: tuple[int, int, int],
    *,
    wrap_width: int | None = None,
    text_y_offset: int = 0,
) -> Image.Image:
    icon = _load_icon(settings.get("icon_path"))
    gap = 2 if icon is not None and text else 0
    if wrap_width is None:
        lines = [text]
    else:
        text_width = max(1, wrap_width - ((icon.width + gap) if icon is not None else 0))
        lines = _wrap_text(text, font, text_width)
    line_h = max(1, _text_bbox(font, "Ag")[1] + 1)
    text_w = max((_text_bbox(font, line)[0] for line in lines), default=0)
    text_h = max(1, len(lines) * line_h)
    width = (icon.width if icon is not None else 0) + gap + text_w
    height = max(PANEL_HEIGHT if icon is not None else 0, text_h + abs(text_y_offset))
    layer = Image.new("RGBA", (max(1, width), max(1, height)), (0, 0, 0, 0))
    x = 0
    if icon is not None:
        layer.paste(icon, (0, max(0, (height - icon.height) // 2)), icon)
        x = icon.width + gap
    y = (height - text_h) // 2 + text_y_offset
    for line in lines:
        mask = Image.new("L", layer.size, 0)
        ImageDraw.Draw(mask).text((x, y), line, font=font, fill=255)
        mask = mask.point(lambda px: 255 if px >= 128 else 0)
        layer.paste(Image.new("RGBA", layer.size, foreground + (255,)), mask=mask)
        y += line_h
    return layer


def _paste_layer(canvas: Image.Image, layer: Image.Image, xy: tuple[int, int]) -> None:
    canvas.paste(layer.convert("RGB"), xy, layer)


def _center_error(text: str, bg: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg)
    font = load_font(DEFAULT_FONT, DEFAULT_FONT_SIZE)
    w, h = _text_bbox(font, text)
    _draw_crisp_text(img, ((PANEL_WIDTH - w) // 2, (PANEL_HEIGHT - h) // 2), text, font, (255, 0, 0))
    return quantize_panel(img)


def render_text_block(settings: dict, text: str, tick: int = 0, *, force_scroll: bool = False) -> Image.Image:
    fg = tuple(settings.get("foreground", DEFAULT_FOREGROUND))
    bg = tuple(settings.get("background", (0, 0, 0)))
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg)
    font = load_font(settings.get("font", DEFAULT_FONT), int(settings.get("font_size", DEFAULT_FONT_SIZE)))
    yoff = int(settings.get("vertical_offset", 0))
    scrolling = settings.get("scrolling", "None (Wrap)")
    if force_scroll:
        scrolling = "Right to left"
    horizontal = scrolling in {"Right to left", "Left to right"}
    vertical = scrolling in {"Top to bottom", "Bottom to top"}
    layer = _icon_and_text_layer(settings, text, font, fg, wrap_width=None if horizontal else PANEL_WIDTH, text_y_offset=yoff)
    speed = max(1, int(settings.get("scroll_speed", 4)))
    if horizontal:
        cycle = max(1, PANEL_WIDTH + layer.width)
        if scrolling == "Right to left":
            x = PANEL_WIDTH - ((tick * speed) % cycle)
        else:
            x = -layer.width + ((tick * speed) % cycle)
        y = (PANEL_HEIGHT - layer.height) // 2
    elif vertical:
        x = (PANEL_WIDTH - layer.width) // 2
        cycle = max(1, PANEL_HEIGHT + layer.height)
        if scrolling == "Top to bottom":
            y = -layer.height + ((tick * speed) % cycle)
        else:
            y = PANEL_HEIGHT - ((tick * speed) % cycle)
    else:
        x = (PANEL_WIDTH - layer.width) // 2
        y = (PANEL_HEIGHT - layer.height) // 2
    _paste_layer(img, layer, (int(x), int(y)))
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
    fg = tuple(settings.get("foreground", DEFAULT_FOREGROUND))
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
            sep = _clock_bitmap("separator.png", bg, bg, settings.get("digit_overrides"))
        elif ch == ":":
            sep = _clock_bitmap("separator.png", fg, bg, settings.get("digit_overrides"))
        else:
            sep = _clock_bitmap(f"digit-{ch}.png", fg, bg, settings.get("digit_overrides"))
        if sep is None:
            return None
        bitmaps.append(sep)
    if suffix:
        suffix_img = _clock_bitmap(suffix, fg, bg, settings.get("digit_overrides"))
        if suffix_img is None:
            return None
        bitmaps.append(suffix_img)
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg)
    icon = _load_icon(settings.get("icon_path"))
    gap = 2 if icon is not None else 0
    total_w = sum(part.width for part in bitmaps) + int(settings.get("digit_spacing", DEFAULT_CLOCK_CHARACTER_SPACING)) * max(0, len(bitmaps) - 1)
    group_w = (icon.width if icon is not None else 0) + gap + total_w
    x = (PANEL_WIDTH - group_w) // 2
    if icon is not None:
        img.paste(icon.convert("RGB"), (x, max(0, (PANEL_HEIGHT - icon.height) // 2)), icon)
        x += icon.width + gap
    for part in bitmaps:
        y = max(0, (PANEL_HEIGHT - part.height) // 2)
        img.paste(part, (x, y))
        x += part.width + int(settings.get("digit_spacing", DEFAULT_CLOCK_CHARACTER_SPACING))
    return quantize_panel(img)


def render_clock(settings: dict, tick: int = 0) -> Image.Image:
    bitmap = _render_bitmap_clock(settings, tick)
    if bitmap is not None:
        return bitmap
    bg = tuple(settings.get("background", (0, 0, 0)))
    fg = tuple(settings.get("foreground", DEFAULT_FOREGROUND))
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg)
    icon = _load_icon(settings.get("icon_path"))
    fmt = "%l:%M" if settings.get("time_mode") == "12-hour" else "%H:%M"
    if settings.get("show_seconds"):
        fmt += ":%S"
    text = time.strftime(fmt)
    if settings.get("flash_separator") and tick % 2:
        text = text.replace(":", " ")
    draw = ImageDraw.Draw(img)
    digit_w = 8
    digit_spacing = int(settings.get("digit_spacing", DEFAULT_CLOCK_CHARACTER_SPACING))
    glyph_widths = [4 if c == ":" else digit_w for c in text]
    width = sum(glyph_widths) + digit_spacing * max(0, len(glyph_widths) - 1)
    gap = 2 if icon is not None else 0
    group_w = (icon.width if icon is not None else 0) + gap + width
    x = (PANEL_WIDTH - group_w) // 2
    if icon is not None:
        img.paste(icon.convert("RGB"), (x, max(0, (PANEL_HEIGHT - icon.height) // 2)), icon)
        x += icon.width + gap
    for ch in text:
        if ch.isdigit():
            draw_digit(draw, x, ch, fg); x += digit_w + digit_spacing
        else:
            if ch == ":":
                draw.rectangle((x + 1, 4, x + 3, 6), fill=fg); draw.rectangle((x + 1, 10, x + 3, 12), fill=fg)
            x += 4 + digit_spacing
    return quantize_panel(img)



def _weather_code_condition(code: int) -> str:
    if code in (0, 1): return "sunny"
    if code in (2, 3): return "cloudy"
    if code in (45, 48): return "foggy"
    if 51 <= code <= 67 or 80 <= code <= 82: return "rainy"
    if 71 <= code <= 77 or 85 <= code <= 86: return "snow"
    if 95 <= code <= 99: return "stormy"
    return "cloudy"


def _fetch_weather(location: str, units: str) -> tuple[str | None, float | None, str | None]:
    normalized = location.strip()
    if not normalized:
        return None, None, "missing"
    key = (normalized.lower(), units)
    cached = _weather_cache.get(key)
    if cached and time.time() - cached[0] < 1800:
        return cached[1], cached[2], cached[3]
    try:
        if normalized.isdigit():
            q = urllib.parse.urlencode({"id": int(normalized), "language": "en", "format": "json"})
            with urllib.request.urlopen(f"https://geocoding-api.open-meteo.com/v1/get?{q}", timeout=8) as response:
                result = json.loads(response.read().decode("utf-8"))
        else:
            q = urllib.parse.urlencode({"name": normalized, "count": 1, "language": "en", "format": "json"})
            with urllib.request.urlopen(f"https://geocoding-api.open-meteo.com/v1/search?{q}", timeout=8) as response:
                geo = json.loads(response.read().decode("utf-8"))
            results = geo.get("results") or []
            if not results:
                _weather_cache[key] = (time.time(), None, None, "missing")
                return None, None, "missing"
            result = results[0]
        if "latitude" not in result or "longitude" not in result:
            _weather_cache[key] = (time.time(), None, None, "missing")
            return None, None, "missing"
        temp_unit = "fahrenheit" if units == "Fahrenheit" else "celsius"
        q = urllib.parse.urlencode({"latitude": result["latitude"], "longitude": result["longitude"], "current": "temperature_2m,weather_code", "temperature_unit": temp_unit})
        with urllib.request.urlopen(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=8) as response:
            weather = json.loads(response.read().decode("utf-8"))
        current = weather.get("current", {})
        condition = _weather_code_condition(int(current.get("weather_code", 3)))
        temp = float(current.get("temperature_2m", 0.0))
        status = None
    except Exception:
        condition, temp, status = None, None, "error"
    _weather_cache[key] = (time.time(), condition, temp, status)
    return condition, temp, status

def _weather_icon(condition: str, foreground: tuple[int, int, int]) -> Image.Image | None:
    path = PACKAGE_DIR / "static" / f"weather-{condition}.png"
    if not path.exists():
        return None
    src = Image.open(path).convert("RGBA").resize((16, 16), Image.Resampling.NEAREST)
    alpha = src.getchannel("A")
    luminance = src.convert("L")
    mask = Image.new("L", src.size, 0)
    for y in range(src.height):
        for x in range(src.width):
            visible = alpha.getpixel((x, y)) > 0 and luminance.getpixel((x, y)) >= 128
            mask.putpixel((x, y), 255 if visible else 0)
    out = Image.new("RGBA", src.size, (0, 0, 0, 0))
    out.paste(Image.new("RGBA", src.size, foreground + (255,)), mask=mask)
    return out


def render_weather(settings: dict) -> Image.Image:
    fg = tuple(settings.get("foreground", DEFAULT_FOREGROUND))
    bg = tuple(settings.get("background", (0, 0, 0)))
    condition, temp, status = _fetch_weather(settings.get("location", ""), settings.get("units", "Celsius"))
    if status == "missing":
        return _center_error("Where?", bg)
    if status == "error" or condition is None or temp is None:
        return _center_error("x", bg)
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg)
    icon = _weather_icon(condition, fg)
    font = load_font(settings.get("font", DEFAULT_FONT), int(settings.get("font_size", DEFAULT_FONT_SIZE)))
    text = f"{condition} {round(temp):d}°"
    text_w, text_h = _text_bbox(font, text)
    gap = 2 if icon is not None else 0
    icon_w = icon.width if icon is not None else 0
    total_w = icon_w + gap + text_w
    x = (PANEL_WIDTH - total_w) // 2
    yoff = int(settings.get("vertical_offset", 0))
    if icon is not None:
        icon_y = (PANEL_HEIGHT - icon.height) // 2
        img.paste(icon.convert("RGB"), (x, icon_y), icon)
    text_y = (PANEL_HEIGHT - text_h) // 2 + yoff
    _draw_crisp_text(img, (x + icon_w + gap, text_y), text, font, fg)
    return quantize_panel(img)

def render_frame(frame: FrameConfig, tick: int = 0) -> Image.Image:
    settings = frame.merged_settings()
    if frame.frame_type is FrameType.TEXT:
        return render_text_block(settings, settings.get("message", ""), tick)
    if frame.frame_type is FrameType.IMAGE:
        return fit_image(settings.get("path"), settings.get("display", "Resize to fit"))
    if frame.frame_type is FrameType.CLOCK:
        return render_clock(settings, tick)
    if frame.frame_type is FrameType.DATE:
        return render_text_block(settings, time.strftime(settings.get("date_format", "%a %d %b")), tick)
    if frame.frame_type is FrameType.WEATHER:
        return render_weather(settings)
    return Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), (0, 0, 0))
