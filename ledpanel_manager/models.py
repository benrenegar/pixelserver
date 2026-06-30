from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PANEL_WIDTH = 96
PANEL_HEIGHT = 16

Color = tuple[int, int, int]


class FrameType(str, Enum):
    TEXT = "Text"
    IMAGE = "Image"
    CLOCK = "Clock"
    DATE = "Date"
    RSS = "RSS Feed"
    LIVE_TEXT = "Live Text"


@dataclass
class FrameConfig:
    frame_type: FrameType = FrameType.TEXT
    duration: float = 10.0
    settings: dict[str, Any] = field(default_factory=dict)

    def defaults(self) -> dict[str, Any]:
        text_base = {
            "foreground": (255, 255, 0),
            "background": (0, 0, 0),
            "font": "VCR OSD Mono",
            "font_size": 16,
            "horizontal_spacing": 0,
            "vertical_offset": 0,
            "icon_path": None,
        }
        if self.frame_type is FrameType.TEXT:
            return text_base | {"message": "Hello", "scrolling": "None (Wrap)", "scroll_speed": 4}
        if self.frame_type is FrameType.IMAGE:
            return {"path": None, "display": "Resize to fit"}
        if self.frame_type is FrameType.CLOCK:
            return {
                "foreground": (255, 255, 0),
                "background": (0, 0, 0),
                "time_mode": "24-hour",
                "show_seconds": False,
                "flash_separator": False,
                "icon_path": None,
            }
        if self.frame_type is FrameType.DATE:
            return text_base | {"date_format": "%d/%m/%Y"}
        if self.frame_type is FrameType.RSS:
            return text_base | {"feed_url": "", "item_count": 5, "scroll_speed": 4}
        if self.frame_type is FrameType.LIVE_TEXT:
            return text_base | {"label": "Value", "rest_url": "", "scrolling": "None (Wrap)", "scroll_speed": 4}
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
