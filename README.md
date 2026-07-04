# LED Matrix Manager

A Python web service for iPixel Color BLE LED matrix panels. 

<img width="1362" height="1033" alt="Screenshot_2026-07-04_17-34-10" src="https://github.com/user-attachments/assets/030d3782-c07a-4eaa-bb11-d35182670ca5" />


## Features

- Runs as a server application with discovery and connection to one or more iPixel Color BLE LED matrix panels.
- Exposes a web application for managing panel output with features including:
  - Playlist management with import and export
  - Text - scrolling text (horizontally and vertically), different fonts and sizes
  - Image - upload an image or draw
  - Clock - with editable digits
  - Date - custom formatting
  - Weather - icon and text for current condition and temperature for specified location
  - Icon, font and color customization
  - Complete icon and image pixel editor
 
<img width="905" height="799" alt="Screenshot_2026-07-04_17-40-38" src="https://github.com/user-attachments/assets/2f9d2496-3c1e-4f58-8bdd-47c9b07c172c" />

<img width="1299" height="615" alt="Screenshot_2026-07-04_17-39-16" src="https://github.com/user-attachments/assets/945835fb-2335-47dd-99d9-4f622977b477" />

<img width="1336" height="464" alt="Screenshot_2026-07-04_17-38-05" src="https://github.com/user-attachments/assets/3662c401-8f5a-414f-a29a-17c66ba8dd28" />

## Assets

Any TTF font can be placed in the fonts/ directory and will be available for selection in the web application. A collection of good pixel fonts is provided.
Custom weather icons and clock digits can be provided as 2-bit PNG image files.

## Running on Debian based Linux

### Install dependenices
```bash
sudo apt update
sudo apt install python3-pil python3-bleak python3-venv python3-setuptools git
```

### Run application

Execute `run.sh` or manually:

```bash
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python3 -m pip install -e . --no-deps --no-build-isolation
python3 -m pip install 'pypixelcolor @ git+https://github.com/lucagoc/pypixelcolor.git'
ledpanel-manager
```
Open `http://localhost:8765/` in a desktop browser. Set `LEDPANEL_PORT` to change the port or `LEDPANEL_HOST` to change the bind address.

## Install application as a systemd service

From the checkout:

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

If Bluetooth permissions prevent access, add the service user to the appropriate bluetooth user group and restart the service:

```bash
sudo usermod -aG bluetooth ledpanel
sudo systemctl restart ledpanel-manager.service
```

## pypixelcolor dependency

The app requires `pypixelcolor` for normal panel updates, it is pulled in automatically when creating the virtual environment.
