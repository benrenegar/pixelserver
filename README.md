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

Debian 13 follows PEP 668, so `python3 -m pip install --user -e .` can fail with `externally-managed-environment`. Use Debian packages for runtime dependencies, then either run from the checkout or install the console script inside a virtual environment.

### Option A: run directly from the checkout

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 python3-pil python3-bleak
python3 -m ledpanel_manager.app
```

### Option B: install an editable console script in a virtual environment

`--system-site-packages` lets the venv use Debian's GTK/Pillow/bleak packages instead of trying to replace them with pip-managed copies.

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 python3-pil python3-bleak python3-venv python3-setuptools
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python3 -m pip install -e . --no-deps --no-build-isolation
ledpanel-manager
```

Do not use `--break-system-packages` for this app; the venv approach keeps Debian's Python installation intact.

The default bundled-font path is `ledpanel_manager/fonts/VCR-OSD-Mono.ttf`. Add the VCR OSD Mono TTF there to make it appear in the font selector; the app also falls back to Pillow's default bitmap font.

## Optional pypixelcolor support

If you want to experiment with the upstream `pypixelcolor` transport, install it inside the venv after the steps above:

```bash
python3 -m pip install pypixelcolor
```

The app will use it when importable and will otherwise fall back to the built-in bleak sender.

## Notes

The protocol layer is intentionally isolated in `ledpanel_manager/ipixel.py` so it can be adapted as upstream projects evolve. It references pypixelcolor/iPixel-CLI and the HA iPixel protocol notes for BLE service UUIDs and device discovery naming.
