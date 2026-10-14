#!/usr/bin/env bash
# run-workers.sh — 3 parallel fastpass workers (folder-disjoint, resume-safe).
#
# PAUSE : Ctrl-C inside each worker window — or just close the lid / reboot.
#         A stop loses at most the one file in flight; nothing else.
# RESUME: run this script again. Workers skip everything already in the manifest.
#
# OLEMI (32,375 drawing files) deliberately omitted 2026-10-14 — not wanted.
# To convert it later: fastpass.py --subtree '<root>/OLEMI'
#
# Guard: refuses to start if a fastpass.py is already running (--force overrides).

set -euo pipefail
REPO="$HOME/engineering-docs-pipeline"
PY="$HOME/pdf-convert-venv/bin/python"
ROOT="/mnt/c/Users/Chris/OneDrive/02. Work"
SESSION=fastpass
MD="$HOME/engineering-md"

if pgrep -f "fastpass.py" >/dev/null && [ "${1:-}" != "--force" ]; then
    echo "A fastpass worker is already running:"
    pgrep -af "fastpass.py" || true
    echo "Pause it first (Ctrl-C in its tmux window), or rerun with --force."
    exit 1
fi

# Folder-disjoint worker assignments (never overlap, or files get done twice).
W1_SUBTREES=("TSP Projects" "OHW Standard Drawings" "RR Technical Info" "Uni Docs" "TSP & ARUP" "Nord Stream 2" "Technical Paper VE" "IStructE Exams" "Malcolm Drilling Technical Papers" "ICE Martime Journal" "NSW Proposals" "Pluit City" "Shirley Downloads" "Pipeline" "Job Description" "ICE Chartership")
W2_SUBTREES=("Geotechnical Documents")
W3_SUBTREES=("Byland Technical" "CMW Projects" "Civil Engineering Books" "Geotechnique" "ICE Professional Review Docs" "Conferences" "M Bolton Publications" "Vancouver Geotechnical Society")

cmd() {  # cmd <logname> <subtree...> -> shell command string with proper quoting
    local log="$1"; shift
    local line="nice -n 10 $PY $REPO/fastpass.py --root '$ROOT'"
    local s
    for s in "$@"; do line+=" --subtree '$ROOT/$s'"; done
    line+=" 2>&1 | tee -a $MD/$log"
    echo "$line"
}

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session  -d -s "$SESSION" -n w1-tsp
tmux new-window   -d -t "$SESSION" -n w2-geotech
tmux new-window   -d -t "$SESSION" -n w3-books
tmux send-keys -t "$SESSION:w1-tsp"     "$(cmd fastpass-w1.log "${W1_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w2-geotech" "$(cmd fastpass-w2.log "${W2_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w3-books"   "$(cmd fastpass-w3.log "${W3_SUBTREES[@]}")" Enter

echo "3 workers launched in tmux session '$SESSION'."
echo "  attach      : tmux attach -t $SESSION"
echo "  switch panes: Ctrl-B n / Ctrl-B p   |   detach: Ctrl-B d"
echo "  progress    : wc -l < $MD/.fastpass.jsonl"
