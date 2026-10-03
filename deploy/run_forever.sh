#!/usr/bin/env bash
# Linux version of run_forever.ps1: keeps the live bot running until a STOP file exists.
# A crash or network outage restarts the bot after 30 s. A halt writes STOP, so it does not restart.
# The API key comes from the environment (systemd EnvironmentFile), never from this repo.
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
export PYTHONIOENCODING=utf-8
PY="${PYTHON:-.venv/bin/python}"

while [ ! -f STOP ]; do
    log="logs/live-$(date +%Y%m%d).txt"
    echo "=== start $(date -Is) ===" >> "$log"
    "$PY" -u execute.py --live >> "$log" 2>&1
    echo "=== bot exited (code $?) $(date -Is) ===" >> "$log"
    [ -f STOP ] && break
    sleep 30
done
echo "=== supervisor stopped $(date -Is) (STOP file present) ===" >> "logs/live-$(date +%Y%m%d).txt"
exit 0
