# LED Matrix Manager

A Debian/Linux GTK client for iPixel Color BLE LED matrix panels, initially targeting 16x64 panels.

## Features

- GTK 4 tabbed UI for one or more panels.
- Background BLE discovery for nearby `LED_BLE_*`/iPixel devices.
- Per-panel frame playlists with text, image, clock, and date frames.
- Live 16x64 pixel preview while display output is running.
- Pillow-based rendering for custom clock/date/images and text preview.
- BLE sender abstraction that uses `pypixelcolor` for panel updates, with the experimental bleak/raw-write transport available only when explicitly enabled for debugging.

## Run on Debian 13

Debian 13 follows PEP 668, so `python3 -m pip install --user -e .` can fail with `externally-managed-environment`. Use Debian packages for runtime dependencies, then either run from the checkout or install the console script inside a virtual environment. The virtual environment option is recommended if you want the app to use `pypixelcolor`, because Debian's `/usr/bin/python3` will not automatically import packages just because a `pypixelcolor` command exists in `~/.local/bin`.

### Option A: run directly from the checkout

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 python3-pil python3-bleak
python3 -m ledpanel_manager.app
```

### Option B: install an editable console script in a virtual environment (recommended for pypixelcolor)

`--system-site-packages` lets the venv use Debian's GTK/Pillow/bleak packages instead of trying to replace them with pip-managed copies.

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 python3-pil python3-bleak python3-venv python3-setuptools
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python3 -m pip install -e . --no-deps --no-build-isolation
python3 -m pip install 'pypixelcolor @ git+https://github.com/lucagoc/pypixelcolor.git'
python3 -c "import pypixelcolor; print(pypixelcolor.__file__)"
ledpanel-manager
```

Do not use `--break-system-packages` for this app; the venv approach keeps Debian's Python installation intact.

If you already have `/home/benrenegar/.local/bin/pypixelcolor`, that confirms the command-line script is on your shell `PATH`, but it does not prove that the Python interpreter running this app can import the `pypixelcolor` module. The check above should print a path inside `.venv` (or another importable site-packages path). If it fails, run the `python3 -m pip install 'pypixelcolor @ git+https://github.com/lucagoc/pypixelcolor.git'` command again while the venv is activated.

The default bundled-font path is `ledpanel_manager/fonts/VCR-OSD-Mono.ttf`. Add the VCR OSD Mono TTF there to make it appear in the font selector; the app also falls back to Pillow's default bitmap font.

## pypixelcolor support

The app requires `pypixelcolor` for normal panel updates. The earlier built-in bleak sender is still present for debugging, but it is disabled by default because it can connect without reliably updating the panel. Prefer Option B above so `pypixelcolor` is installed into the app venv from the upstream GitHub repository.

Useful diagnostics:

```bash
which pypixelcolor
python3 -c "import sys; print(sys.executable); import pypixelcolor; print(pypixelcolor.__file__)"
```

If `which pypixelcolor` succeeds but the Python import fails, the CLI script and the running Python environment do not match. Activate `.venv` and install the module with `python3 -m pip install 'pypixelcolor @ git+https://github.com/lucagoc/pypixelcolor.git'`. The app imports `pypixelcolor.AsyncClient`, which is what the current package exports.

If pip reports `BackendUnavailable: Cannot import 'hatchling.build'`, it means `pypixelcolor` needs the Hatchling build backend. Do not install `pypixelcolor` with `--no-build-isolation`; run the separate `python3 -m pip install 'pypixelcolor @ git+https://github.com/lucagoc/pypixelcolor.git'` command above so pip can create an isolated build environment and install Hatchling for that package automatically.

To temporarily re-enable the experimental bleak sender for protocol debugging, launch the app with `LEDPANEL_ALLOW_BLEAK_FALLBACK=1 ledpanel-manager`.

## Notes

The protocol layer is intentionally isolated in `ledpanel_manager/ipixel.py` so it can be adapted as upstream projects evolve. It references pypixelcolor/iPixel-CLI and the HA iPixel protocol notes for BLE service UUIDs and device discovery naming.
