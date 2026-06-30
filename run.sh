#!/bin/bash

python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python3 -m pip install -e . --no-deps --no-build-isolation
python3 -m pip install 'pypixelcolor @ git+https://github.com/lucagoc/pypixelcolor.git'
python3 -c "import pypixelcolor; print(pypixelcolor.__file__)"
ledpanel-manager

exit 0