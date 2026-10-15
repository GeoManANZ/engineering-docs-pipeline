#!/usr/bin/env bash
# resume-fastpass.sh — relaunch all fastpass workers after a WSL restart.
# Safe to re-run: fastpass skips files already classified "ok*" in the manifest.
# Usage:  bash /home/chrb/engineering-docs-pipeline/resume-fastpass.sh
set -uo pipefail

CMD_FILE=/home/chrb/engineering-docs-pipeline/fastpass-workers.cmd
LOG_DIR=/home/chrb/engineering-md
PY=/home/chrb/pdf-convert-venv/bin/python

if [[ ! -s "$CMD_FILE" ]]; then
  echo "ERROR: $CMD_FILE missing or empty" >&2
  exit 1
fi

n=0
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  n=$((n+1))
  log="$LOG_DIR/fastpass-resume-w${n}.log"
  # first path token after the script name is usually the first --subtree; keep short name
  tag=$(echo "$line" | grep -o -- '--subtree [^ ]*' | head -1 | sed 's|--subtree /mnt/c/Users/Chris/OneDrive/02. Work/||' | tr -cd '[:alnum:]' | cut -c1-20)
  nohup "$PY" $line > "$LOG_DIR/fastpass-w-${tag:-r$n}.log" 2>&1 &
  echo "launched worker $n (pid $!) tag=${tag:-r$n}"
  sleep 1
done < "$CMD_FILE"

echo "launched $n workers. Tail logs in $LOG_DIR/fastpass-w-*.log"
