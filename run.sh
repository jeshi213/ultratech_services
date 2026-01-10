#!/bin/bash
cd "$(dirname "$0")"
pip3 install --break-system-packages -r requirements.txt --upgrade 2>/dev/null || pip install -r requirements.txt --upgrade
python3 app.py
