#!/usr/bin/env bash
# vps-pull.sh — sync VPS conversions back to the laptop.
#   .md tree  : merged into ~/engineering-md (same relative paths)
#   manifest  : VPS records APPENDED to the laptop manifest (only new lines,
#               tracked via a line-count marker). Later-wins semantics apply.
set -euo pipefail

MD="$HOME/engineering-md"
MARKER="$MD/.vps-manifest-lines"

rsync -a --exclude='.fastpass.jsonl' vps:'~/fastpass-vps/out/' "$MD/"

total=$(ssh vps 'wc -l < ~/fastpass-vps/out/.fastpass.jsonl 2>/dev/null || echo 0')
pulled=0
[ -f "$MARKER" ] && pulled=$(cat "$MARKER")

if [ "$total" -gt "$pulled" ]; then
  ssh vps "tail -n +$((pulled + 1)) ~/fastpass-vps/out/.fastpass.jsonl" >> "$MD/.fastpass.jsonl"
  echo "$total" > "$MARKER"
  echo "merged $((total - pulled)) new manifest records (vps total: $total)"
else
  echo "no new manifest records (vps total: $total, already merged: $pulled)"
fi
echo "done — laptop manifest now: $(wc -l < "$MD/.fastpass.jsonl") lines"
