from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable
from PIL import Image

WRITE_UUID = "0000fa02-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fa03-0000-1000-8000-00805f9b34fb"
DEVICE_PREFIXES = ("LED_BLE", "iPixel", "B.K.Light", "BGLight")


@dataclass(frozen=True)
class DiscoveredPanel:
    name: str
    address: str


def image_to_rgb565_bytes(image: Image.Image) -> bytes:
    data = bytearray()
    for r, g, b in image.convert("RGB").getdata():
        value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        data.extend(value.to_bytes(2, "big"))
    return bytes(data)


class IPixelClient:
    """Small adapter around pypixelcolor/bleak for 16x64 bitmap updates."""

    def __init__(self, address: str):
        self.address = address
        self._client = None
        self._pypixel = None

    async def connect(self) -> None:
        try:
            from pypixelcolor import IPixelDevice  # type: ignore
            self._pypixel = IPixelDevice(self.address)
            maybe = self._pypixel.connect()
            if asyncio.iscoroutine(maybe): await maybe
            return
        except Exception:
            self._pypixel = None
        from bleak import BleakClient
        self._client = BleakClient(self.address)
        await self._client.connect()

    async def disconnect(self) -> None:
        target = self._pypixel or self._client
        if target is None: return
        maybe = target.disconnect()
        if asyncio.iscoroutine(maybe): await maybe

    async def send_image(self, image: Image.Image) -> None:
        if self._pypixel is not None:
            for name in ("send_image", "draw_image", "set_image"):
                method = getattr(self._pypixel, name, None)
                if method:
                    maybe = method(image)
                    if asyncio.iscoroutine(maybe): await maybe
                    return
        if self._client is None:
            raise RuntimeError("Panel is not connected")
        payload = b"\x05\x00\x44" + image_to_rgb565_bytes(image)
        for start in range(0, len(payload), 180):
            await self._client.write_gatt_char(WRITE_UUID, payload[start:start + 180], response=False)
            await asyncio.sleep(0.01)


async def discover_panels(on_panel: Callable[[DiscoveredPanel], None], timeout: float = 5.0) -> None:
    from bleak import BleakScanner
    devices = await BleakScanner.discover(timeout=timeout)
    for dev in devices:
        name = dev.name or ""
        if any(name.startswith(prefix) for prefix in DEVICE_PREFIXES):
            on_panel(DiscoveredPanel(name=name, address=dev.address))
