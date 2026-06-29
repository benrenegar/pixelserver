# LED Matrix Manager

A Debian/Linux GTK client for iPixel Color BLE LED matrix panels, initially targeting 16x64 panels.

## Features

- GTK 4 tabbed UI for one or more panels.
- Background BLE discovery for nearby `LED_BLE_*`/iPixel devices.
- Per-panel frame playlists with text, image, clock, and date frames.
- Live 16x64 pixel preview while display output is running.
- Pillow-based rendering for custom clock/date/images and text preview.
- BLE sender abstraction that uses `pypixelcolor` when available and falls back to documented iPixel BLE UUID/raw-write structure.

## Run on Debian 13

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 python3-pil python3-bleak
python3 -m pip install --user -e .
ledpanel-manager
```

The default bundled-font path is `ledpanel_manager/fonts/VCR-OSD-Mono.ttf`. Add the VCR OSD Mono TTF there to make it appear in the font selector; the app also falls back to Pillow's default bitmap font.

## Notes

The protocol layer is intentionally isolated in `ledpanel_manager/ipixel.py` so it can be adapted as upstream projects evolve. It references pypixelcolor/iPixel-CLI and the HA iPixel protocol notes for BLE service UUIDs and device discovery naming.
