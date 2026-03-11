#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

source env/bin/activate

nohup python3 -u record.py > output.log 2>&1 &
echo "Bird Recorder started (PID $!) — http://$(hostname -I | awk '{print $1}'):5000"
