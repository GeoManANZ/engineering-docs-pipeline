#!/usr/bin/env bash
# duty-cycle.sh — freeze all fastpass workers 10 min per hour (heat-soak control).
# SIGSTOP/SIGCONT: zero loss, unlike kill. Exits when no workers remain.
while true; do
    sleep 3000
    pgrep -f "venv/bin/python.*fastpass" >/dev/null || exit 0
    pkill -STOP -f "pdf-convert-venv/bin/python"
    echo "$(date '+%F %T') cooldown: workers frozen 10 min"
    sleep 600
    pkill -CONT -f "pdf-convert-venv/bin/python"
done
