# LED Matrix Manager

A Debian/Raspberry Pi OS web service for iPixel Color BLE LED matrix panels, targeting 16x96 panels.

## Features

- Long-running server application suitable for `systemd`.
- Browser-based desktop UI served on port `8765` by default, with tabbed panel management and an inline status console.
- BLE discovery for nearby `LED_BLE_*`/iPixel devices, including a web UI Scan button.
- Per-panel frame playlists with Text, Image, Clock, and Date frames.
- Automatic panel reconnects after send failures and automatic reconnect on service start when a saved panel address exists.
- Canvas LED previews using dim grey circles for off pixels.
- Pillow-based rendering for custom clock/date/images and text preview.
- Primitive browser pixel editor for image frames, plus browser file uploads for images and icons.
- Clock digit pixel editor with per-digit bitmap overrides and configurable digit spacing.

## Assets

Font selectors are populated from TTF files in `ledpanel_manager/fonts/`. The service exposes those files as web fonts so the bundled fonts remain available to the browser UI and the Pillow renderer.

Clock bitmap mode looks for `digit-0.png` through `digit-9.png`, `separator.png`, `am.png`, and `pm.png` in `ledpanel_manager/digits/`. Digit edits made in the web UI are saved beneath the service config directory and override the bundled files for that frame. Web UI styles live in `ledpanel_manager/static/app.css` so deployments can customize or override the browser appearance there. Weather icons live as editable 16x16 PNG files in `ledpanel_manager/static/weather-*.png`.

## Run on Debian 13 / Raspberry Pi OS

Debian 13 follows PEP 668, so use Debian packages for runtime dependencies and install only the app and `pypixelcolor` inside a virtual environment.

```bash
sudo apt update
sudo apt install python3-pil python3-bleak python3-venv python3-setuptools git
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python3 -m pip install -e . --no-deps --no-build-isolation
python3 -m pip install 'pypixelcolor @ git+https://github.com/lucagoc/pypixelcolor.git'
ledpanel-manager
```

Open `http://<raspberry-pi-hostname-or-ip>:8765/` in a desktop browser. Set `LEDPANEL_PORT` to change the port or `LEDPANEL_HOST` to change the bind address.

## Install as a systemd service

From the checkout on the Raspberry Pi:

```bash
sudo useradd --system --create-home --home-dir /var/lib/ledpanel --shell /usr/sbin/nologin ledpanel || true
sudo install -d -o ledpanel -g ledpanel /opt/ledpanel-manager
sudo rsync -a --delete ./ /opt/ledpanel-manager/
sudo -u ledpanel python3 -m venv --system-site-packages /opt/ledpanel-manager/.venv
sudo -u ledpanel /opt/ledpanel-manager/.venv/bin/python -m pip install -e /opt/ledpanel-manager --no-deps --no-build-isolation
sudo -u ledpanel /opt/ledpanel-manager/.venv/bin/python -m pip install 'pypixelcolor @ git+https://github.com/lucagoc/pypixelcolor.git'
```

Create `/etc/systemd/system/ledpanel-manager.service`:

```ini
[Unit]
Description=LED Matrix Manager web service
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target

[Service]
Type=simple
User=ledpanel
Group=ledpanel
WorkingDirectory=/opt/ledpanel-manager
Environment=LEDPANEL_HOST=0.0.0.0
Environment=LEDPANEL_PORT=8765
Environment=LEDPANEL_CONFIG_DIR=/var/lib/ledpanel/.config/ledpanel-manager
ExecStart=/opt/ledpanel-manager/.venv/bin/ledpanel-manager
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ledpanel-manager.service
sudo systemctl status ledpanel-manager.service
```

If Bluetooth permissions prevent access, add the service user to the appropriate Raspberry Pi OS Bluetooth group and restart the service:

```bash
sudo usermod -aG bluetooth ledpanel
sudo systemctl restart ledpanel-manager.service
```

## pypixelcolor support

The app requires `pypixelcolor` for normal panel updates. The experimental bleak sender is still present for debugging, but disabled by default because it can connect without reliably updating the panel.

Useful diagnostics:

```bash
which pypixelcolor
python3 -c "import sys; print(sys.executable); import pypixelcolor; print(pypixelcolor.__file__)"
```

To temporarily re-enable the experimental bleak sender for protocol debugging, launch the service with `LEDPANEL_ALLOW_BLEAK_FALLBACK=1`.
