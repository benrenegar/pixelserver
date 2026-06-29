from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

PANEL_WIDTH = 64
PANEL_HEIGHT = 16

Color = tuple[int, int, int]


class FrameType(str, Enum):
    TEXT = "Text"
    IMAGE = "Image"
    CLOCK = "Clock"
    DATE = "Date"


@dataclass
class FrameConfig:
    frame_type: FrameType = FrameType.TEXT
    duration: float = 10.0
    settings: dict[str, Any] = field(default_factory=dict)

    def defaults(self) -> dict[str, Any]:
        base = {
            "foreground": (255, 255, 0),
            "background": (0, 0, 0),
            "font": "VCR OSD Mono",
            "font_size": 16,
        }
        if self.frame_type is FrameType.TEXT:
            return base | {"message": "Hello", "scrolling": "None", "scroll_speed": 4}
        if self.frame_type is FrameType.IMAGE:
            return {"path": None, "display": "Resize to fit"}
        if self.frame_type is FrameType.CLOCK:
            return {"foreground": (255, 255, 0), "background": (0, 0, 0), "time_mode": "24-hour", "show_seconds": False, "flash_separator": False}
        if self.frame_type is FrameType.DATE:
            return base | {"date_format": "%d/%m/%Y"}
        return base

    def merged_settings(self) -> dict[str, Any]:
        return self.defaults() | self.settings


@dataclass
class PanelState:
    name: str
    address: str | None = None
    connected: bool = False
    frames: list[FrameConfig] = field(default_factory=lambda: [FrameConfig()])
    running: bool = False
