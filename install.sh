#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
echo "Installed. Edit config.yaml, then run: source .venv/bin/activate && uvicorn voice_bridge.app:app --host 127.0.0.1 --port 8010"
