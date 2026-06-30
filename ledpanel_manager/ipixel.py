from __future__ import annotations

import asyncio
import binascii
import importlib.util
import io
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable
from PIL import Image

WRITE_UUID = "0000fa02-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fa03-0000-1000-8000-00805f9b34fb"
DEVICE_PREFIXES = ("LED_BLE", "iPixel", "B.K.Light", "BGLight")
PNG_CHUNK_SIZE = 244
ACK_TIMEOUT = 8.0
LIVE_SCREEN_BUFFER = 0x65
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredPanel:
    name: str
    address: str


def _switch_endian(hex_string: str) -> str:
    return "".join(reversed([hex_string[i:i + 2] for i in range(0, len(hex_string), 2)]))


def _frame_size_hex(data_hex: str, width: int) -> str:
    return _switch_endian(hex(len(data_hex) // 2)[2:].zfill(width))


def _crc32_hex(data_hex: str) -> str:
    crc = binascii.crc32(bytes.fromhex(data_hex)) & 0xFFFFFFFF
    return _switch_endian(f"{crc:08x}")


def image_to_png_command(image: Image.Image, buffer_number: int = LIVE_SCREEN_BUFFER) -> bytes:
    """Build the iPixel static PNG command used by iPixel-CLI/send_png."""
    output = io.BytesIO()
    image.convert("RGBA").save(output, format="PNG", compress_level=6)
    png_hex = output.getvalue().hex()
    size = _frame_size_hex(png_hex, 8)
    checksum = _crc32_hex(png_hex)
    frame_hex = "020000" + size + checksum + f"00{buffer_number & 0xFF:02x}" + png_hex
    prefix = _frame_size_hex("FFFF" + frame_hex, 4)
    command = bytes.fromhex(prefix + frame_hex)
    logger.debug("Built PNG command: png_bytes=%d command_bytes=%d crc=%s", len(output.getvalue()), len(command), checksum)
    return command



def select_screen_command(screen_number: int = LIVE_SCREEN_BUFFER) -> bytes:
    return bytes([0x05, 0x00, 0x07, 0x80, screen_number & 0xFF])

def image_to_rgb565_bytes(image: Image.Image) -> bytes:
    data = bytearray()
    for r, g, b in image.convert("RGB").getdata():
        value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        data.extend(value.to_bytes(2, "big"))
    return bytes(data)


class _AckManager:
    def __init__(self) -> None:
        self.window_event = asyncio.Event()
        self.all_event = asyncio.Event()

    def reset(self) -> None:
        self.window_event.clear()
        self.all_event.clear()

    def notify(self, _: int, data: bytearray) -> None:
        payload = bytes(data)
        logger.debug("BLE notify: %s", payload.hex())
        if len(payload) >= 5 and payload[0] == 0x05:
            if payload[4] in (0, 1, 3):
                self.window_event.set()
            if payload[4] == 3:
                self.all_event.set()


class IPixelClient:
    """Small adapter around pypixelcolor/bleak for 16x64 bitmap updates."""

    def __init__(self, address: str):
        self.address = address
        self._client = None
        self._pypixel = None
        self._ack = _AckManager()
        self._notify_started = False

    async def connect(self) -> None:
        try:
            from pypixelcolor import AsyncClient  # type: ignore
        except ModuleNotFoundError as exc:
            logger.error(
                "pypixelcolor is not importable by %s. Install it into this environment; "
                "the bleak fallback is disabled by default because it does not reliably update this panel.",
                sys.executable,
            )
            logger.debug("pypixelcolor import spec: %r", importlib.util.find_spec("pypixelcolor"))
            if not self._allow_bleak_fallback():
                raise RuntimeError("pypixelcolor is required for panel updates but is not installed in this Python environment") from exc
            await self._connect_bleak()
            return
        except ImportError as exc:
            logger.error(
                "Installed pypixelcolor package does not expose the expected AsyncClient API; "
                "using bleak only if LEDPANEL_ALLOW_BLEAK_FALLBACK=1",
                exc_info=True,
            )
            if not self._allow_bleak_fallback():
                raise RuntimeError("Installed pypixelcolor package is incompatible; expected pypixelcolor.AsyncClient") from exc
            await self._connect_bleak()
            return
        try:
            self._pypixel = AsyncClient(self.address)
            await self._pypixel.connect()
            logger.info("Connected to %s using pypixelcolor AsyncClient", self.address)
            return
        except Exception:
            logger.exception("pypixelcolor connection failed")
            self._pypixel = None
            if not self._allow_bleak_fallback():
                raise
            logger.warning("Falling back to bleak because LEDPANEL_ALLOW_BLEAK_FALLBACK=1")
            await self._connect_bleak()

    def _allow_bleak_fallback(self) -> bool:
        return os.environ.get("LEDPANEL_ALLOW_BLEAK_FALLBACK") == "1"

    async def _connect_bleak(self) -> None:
        from bleak import BleakClient
        self._client = BleakClient(self.address)
        await self._client.connect()
        try:
            await self._client.start_notify(NOTIFY_UUID, self._ack.notify)
            self._notify_started = True
        except Exception:
            logger.warning("Unable to enable notify characteristic %s", NOTIFY_UUID, exc_info=True)
        await self._write_command(bytes.fromhex("0500070101"), wait_for_ack=False)
        await self._write_command(bytes.fromhex("0500040101"), wait_for_ack=False)
        logger.info("Connected to %s using bleak", self.address)

    async def disconnect(self) -> None:
        target = self._pypixel or self._client
        if target is None:
            return
        if self._client is not None and self._notify_started:
            try:
                await self._client.stop_notify(NOTIFY_UUID)
            except Exception:
                logger.debug("Failed to stop BLE notifications", exc_info=True)
        maybe = target.disconnect()
        if asyncio.iscoroutine(maybe):
            await maybe

    async def send_image(self, image: Image.Image) -> None:
        if self._pypixel is not None:
            with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
                image.convert("RGBA").save(tmp.name, format="PNG", compress_level=6)
                logger.debug("Sending image using pypixelcolor.AsyncClient.send_image")
                await self._pypixel.send_image(tmp.name, resize_method="fit")
            return
        command = image_to_png_command(image)
        await self._write_command(command)
        await self._write_command(select_screen_command(LIVE_SCREEN_BUFFER), wait_for_ack=False)

    async def _write_command(self, data: bytes, *, wait_for_ack: bool = True) -> None:
        if self._client is None:
            raise RuntimeError("Panel is not connected")
        logger.debug("Writing command: bytes=%d first32=%s", len(data), data[:32].hex())
        self._ack.reset()
        for start in range(0, len(data), PNG_CHUNK_SIZE):
            chunk = data[start:start + PNG_CHUNK_SIZE]
            logger.debug("BLE write chunk offset=%d size=%d", start, len(chunk))
            await self._client.write_gatt_char(WRITE_UUID, chunk, response=True)
        if wait_for_ack and self._notify_started:
            try:
                await asyncio.wait_for(self._ack.window_event.wait(), timeout=ACK_TIMEOUT)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"No BLE ACK from display after writing {len(data)} bytes") from exc


async def discover_panels(on_panel: Callable[[DiscoveredPanel], None], timeout: float = 5.0) -> None:
    from bleak import BleakScanner
    devices = await BleakScanner.discover(timeout=timeout)
    for dev in devices:
        name = dev.name or ""
        if any(name.startswith(prefix) for prefix in DEVICE_PREFIXES):
            on_panel(DiscoveredPanel(name=name, address=dev.address))
