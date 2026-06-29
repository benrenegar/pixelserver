from __future__ import annotations

from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
from .models import PANEL_HEIGHT, PANEL_WIDTH, FrameConfig, FrameType

FONT_DIR = Path(__file__).with_name("fonts")
FONT_ALIASES = {"Phatone": FONT_DIR / "phatone.ttf", 
"Loxica": FONT_DIR / "v5loxicar.ttf",
"Square Dance": FONT_DIR / "squaredance00.ttf"}
DIGITS = {
    "0": ["111","101","101","101","111"], "1": ["010","110","010","010","111"],
    "2": ["111","001","111","100","111"], "3": ["111","001","111","001","111"],
    "4": ["101","101","111","001","001"], "5": ["111","100","111","001","111"],
    "6": ["111","100","111","101","111"], "7": ["111","001","001","001","001"],
    "8": ["111","101","111","101","111"], "9": ["111","101","111","001","111"],
    ":": ["0","1","0","1","0"], "/": ["001","001","010","100","100"], "-": ["000","000","111","000","000"], " ": ["0","0","0","0","0"],
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


def render_text(settings: dict, offset: int = 0) -> Image.Image:
    fg, bg = settings["foreground"], settings["background"]
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg)
    draw = ImageDraw.Draw(img)
    font = load_font(settings.get("font", "VCR OSD Mono"), int(settings.get("font_size", 16)))
    text = settings.get("message", "")
    bbox = draw.textbbox((0, 0), text, font=font)
    x, y = 0, (PANEL_HEIGHT - (bbox[3] - bbox[1])) // 2 - bbox[1]
    scrolling = settings.get("scrolling", "None")
    if scrolling == "Right to left": x = PANEL_WIDTH - offset
    elif scrolling == "Left to right": x = offset - (bbox[2] - bbox[0])
    elif scrolling == "Top to bottom": y = offset - (bbox[3] - bbox[1])
    elif scrolling == "Bottom to top": y = PANEL_HEIGHT - offset
    draw.text((x, y), text, fill=fg, font=font)
    return quantize_panel(img)


def render_clock(settings: dict, now: datetime | None = None) -> Image.Image:
    now = now or datetime.now()
    fmt = "%H:%M:%S" if settings.get("show_seconds") else "%H:%M"
    if settings.get("time_mode") == "12-hour": fmt = "%I:%M:%S" if settings.get("show_seconds") else "%I:%M"
    text = now.strftime(fmt).lstrip("0") or "0"
    if settings.get("flash_separator") and now.second % 2: text = text.replace(":", " ")
    return render_dot_text(text, settings["foreground"], settings["background"])


def render_dot_text(text: str, fg, bg) -> Image.Image:
    scale, gap = 2, 1
    widths = [(len(DIGITS.get(ch, DIGITS[' '])[0]) * scale) for ch in text]
    total_w = sum(widths) + gap * (len(text) - 1)
    x = max(0, (PANEL_WIDTH - total_w) // 2); y = 2
    img = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), bg); draw = ImageDraw.Draw(img)
    for ch, w in zip(text, widths):
        pat = DIGITS.get(ch, DIGITS[" "])
        for row, line in enumerate(pat):
            for col, val in enumerate(line):
                if val == "1": draw.rectangle((x+col*scale, y+row*scale, x+col*scale+1, y+row*scale+1), fill=fg)
        x += w + gap
    return img


def render_date(settings: dict, now: datetime | None = None) -> Image.Image:
    settings = settings | {"message": (now or datetime.now()).strftime(settings.get("date_format", "%d/%m/%Y")), "scrolling": "None"}
    return render_text(settings)


def render_frame(frame: FrameConfig, tick: int = 0) -> Image.Image:
    s = frame.merged_settings()
    if frame.frame_type is FrameType.IMAGE: return fit_image(s.get("path"), s.get("display", "Resize to fit"))
    if frame.frame_type is FrameType.CLOCK: return render_clock(s)
    if frame.frame_type is FrameType.DATE: return render_date(s)
    return render_text(s, tick * max(1, int(s.get("scroll_speed", 4))))
