#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "Step 1/2: acoustic PID calibration (about 60 seconds)."
echo "Keep the USB microphone stationary and the speakers connected."
python3 calibrate_sync.py

echo
echo "Step 2/2: starting receiver audio on all three synchronized speakers."
python3 run_hub.py
