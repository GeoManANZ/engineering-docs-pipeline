#!/usr/bin/env bash
# run-workers-safe.sh — 4 workers, disjoint core slices, ~1/3 of CPU.
# Robustness mode after the 2026-10-16 NVIDIA BSOD (bugcheck 0x113, vendor 0x10de).
# Full-power variant: run-workers.sh. Duty-cycle cooldowns: duty-cycle.sh.
set -euo pipefail
SESSION=fastpass
REPO="$HOME/engineering-docs-pipeline"

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION"
tmux rename-window -t "$SESSION" safe-w1
tmux send-keys -t "$SESSION:safe-w1" "bash /home/chrb/engineering-docs-pipeline/workers/safe-w1.sh" Enter
tmux new-window -d -t "$SESSION" -n safe-w2
tmux send-keys -t "$SESSION:safe-w2" "bash /home/chrb/engineering-docs-pipeline/workers/safe-w2.sh" Enter
tmux new-window -d -t "$SESSION" -n safe-w3
tmux send-keys -t "$SESSION:safe-w3" "bash /home/chrb/engineering-docs-pipeline/workers/safe-w3.sh" Enter
tmux new-window -d -t "$SESSION" -n safe-w4
tmux send-keys -t "$SESSION:safe-w4" "bash /home/chrb/engineering-docs-pipeline/workers/safe-w4.sh" Enter
echo "4 safe workers launched (cores 0-11, caps on). Duty cycle: nohup bash $REPO/duty-cycle.sh >/dev/null 2>&1 &"
nohup bash "$REPO/duty-cycle.sh" >/dev/null 2>&1 &
echo "  progress: wc -l < $HOME/engineering-md/.fastpass.jsonl"
