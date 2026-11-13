#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
rm -rf .venv
echo "Removed the project virtual environment. Project files and config.yaml were retained."
