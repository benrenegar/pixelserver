from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PANEL_WIDTH = 96
PANEL_HEIGHT = 16

Color = tuple[int, int, int]
DEFAULT_FONT = "Avante 8"
DEFAULT_FONT_SIZE = 8
DEFAULT_FOREGROUND: Color = (0, 255, 179)


class FrameType(str, Enum):
    TEXT = "Text"
    IMAGE = "Image"
    CLOCK = "Clock"
    DATE = "Date"
    WEATHER = "Weather"


@dataclass
class FrameConfig:
    frame_type: FrameType = FrameType.TEXT
    duration: float = 10.0
    settings: dict[str, Any] = field(default_factory=dict)

    def defaults(self) -> dict[str, Any]:
        text_base = {
            "foreground": DEFAULT_FOREGROUND,
            "background": (0, 0, 0),
            "font": DEFAULT_FONT,
            "font_size": DEFAULT_FONT_SIZE,
            "vertical_offset": 0,
            "icon_path": None,
        }
        if self.frame_type is FrameType.TEXT:
            return text_base | {"message": "Hello", "scrolling": "None (Wrap)", "scroll_speed": 4}
        if self.frame_type is FrameType.IMAGE:
            return {"path": None, "display": "Resize to fit"}
        if self.frame_type is FrameType.CLOCK:
            return {
                "foreground": DEFAULT_FOREGROUND,
                "background": (0, 0, 0),
                "time_mode": "24-hour",
                "show_seconds": False,
                "flash_separator": False,
                "icon_path": None,
                "digit_spacing": 2,
                "digit_overrides": {},
            }
        if self.frame_type is FrameType.DATE:
            return text_base | {"date_format": "%a %d %b"}
        if self.frame_type is FrameType.WEATHER:
            return text_base | {"location": "2149797", "units": "Celsius"}
        return text_base

    def merged_settings(self) -> dict[str, Any]:
        merged = self.defaults() | self.settings
        if "vertical_spacing" in merged and "vertical_offset" not in self.settings:
            merged["vertical_offset"] = merged.pop("vertical_spacing")
        return merged


@dataclass
class PanelState:
    name: str
    address: str | None = None
    connected: bool = False
    frames: list[FrameConfig] = field(default_factory=lambda: [FrameConfig()])
    running: bool = False
    brightness: int = 80
    started_at: float | None = None
    blanked: bool = False
