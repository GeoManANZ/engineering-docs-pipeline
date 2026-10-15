#!/usr/bin/env bash
# run-workers.sh — 8 parallel fastpass workers (folder-disjoint, resume-safe).
#
# PAUSE : Ctrl-C inside each worker window — or just close the lid / reboot.
# RESUME: run this script again. Workers skip everything already in the manifest.
#
# OLEMI (32,375 drawing files) deliberately omitted 2026-10-14 — not wanted.
# 8-worker layout (2026-10-15, user away): TSP solo, misc solo, Geotech thirds,
# Byland solo, books/rest split. GPU is reserved for Stage-2 MinerU OCR.
# Scale back: Ctrl-C extra windows, or edit the arrays and relaunch.

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

W_TSP_SUBTREES=("TSP Projects")
W_MISC_SUBTREES=("OHW Standard Drawings" "RR Technical Info" "Uni Docs" "TSP & ARUP" "Nord Stream 2" "Technical Paper VE" "IStructE Exams" "Malcolm Drilling Technical Papers" "ICE Martime Journal" "NSW Proposals" "Pluit City" "Shirley Downloads" "Pipeline" "Job Description" "ICE Chartership")
W2A_SUBTREES=("Geotechnical Documents/Australian Geomechanics Journal" "Geotechnical Documents/3rd Conf Earthquake" "Geotechnical Documents/Tasmanian Government USB" "Geotechnical Documents/Australian Standards" "Geotechnical Documents/BHe HardDrive" "Geotechnical Documents/ICE Ground Improvement" "Geotechnical Documents/Victoria Basin Studies" "Geotechnical Documents/Humes Products" "Geotechnical Documents/Ground Engineering Magazine" "Geotechnical Documents/CIRIA GUIDES" "Geotechnical Documents/West Gate Geotech" "Geotechnical Documents/Rankine Lecture" "Geotechnical Documents/Arup Geoguides" "Geotechnical Documents/Bamford Rock Testing" "Geotechnical Documents/CSW" "Geotechnical Documents/NSW" "Geotechnical Documents/Rock Netting" "Geotechnical Documents/Geotechnical Baseline Reports + Papers" "Geotechnical Documents/Steel Piling Group Tech Notes" "Geotechnical Documents/CRE Compentency" "Geotechnical Documents/Rock Embankment Technical Papers" "Geotechnical Documents/Doherty Papers" "Geotechnical Documents/Top ICE 10 Papers 2019" "Geotechnical Documents/Elemex" "Geotechnical Documents/GWEP ICE Proceedings" "Geotechnical Documents/Metro Wall" "Geotechnical Documents/GeoCongress 2024" "Geotechnical Documents/Point Load Tests" "Geotechnical Documents/Horizontal directional Drilling" "Geotechnical Documents/Deep Vibratory Compaction" "Geotechnical Documents/Pipe Jacking" "Geotechnical Documents/CMW Ground Models" "Geotechnical Documents/Foundation Drilling Magazine" "Geotechnical Documents/03 CYP Procedures" "Geotechnical Documents/Code of Practice for Project Management (Construction)" "Geotechnical Documents/Engineering Design Representation and Reasoning" "Geotechnical Documents/Integrity Testing" "Geotechnical Documents/Textbook of Engineering Drawing by K. Venkata Reddy")
W2B_SUBTREES=("Geotechnical Documents/TSP Design Software and Information" "Geotechnical Documents/GRL Engineers" "Geotechnical Documents/ICE Geo Engineering Journal" "Geotechnical Documents/ICE Civil Engineering" "Geotechnical Documents/Geology Maps" "Geotechnical Documents/Road & Pavement Design" "Geotechnical Documents/International Journal of Geoengineering Case Histories" "Geotechnical Documents/IJCH Papers" "Geotechnical Documents/Sheet Piling Drgs TSP" "Geotechnical Documents/PileBuck" "Geotechnical Documents/CMW Review" "Geotechnical Documents/ICE Technical Papers" "Geotechnical Documents/Geofabrics" "Geotechnical Documents/Dredging Papers" "Geotechnical Documents/NCE" "Geotechnical Documents/Principles_of_Geotechnical_Engineering_8th_edition_Das_Sobhan_Solution_Manual" "Geotechnical Documents/AGEC" "Geotechnical Documents/Vibropile South Yarra" "Geotechnical Documents/Propex Erosion Information" "Geotechnical Documents/Ischebeck" "Geotechnical Documents/Rock Engineering" "Geotechnical Documents/Stab Sand" "Geotechnical Documents/Blue Johannah Road GI and Design" "Geotechnical Documents/Example Metro Drawings" "Geotechnical Documents/ICE SPERW 3" "Geotechnical Documents/Micropile Earth Retention" "Geotechnical Documents/Melbourne East West Link" "Geotechnical Documents/Deflectometer" "Geotechnical Documents/E & C Magazine" "Geotechnical Documents/OHS Vic" "Geotechnical Documents/Roger Bullivant Design" "Geotechnical Documents/EFFC" "Geotechnical Documents/Keynetix" "Geotechnical Documents/Boskalis Spreadsheets" "Geotechnical Documents/Davis -  Geo Estructural" "Geotechnical Documents/Engineering with Excel, 4th Edition" "Geotechnical Documents/Pile Cap - Tucker" "Geotechnical Documents/Well Licence")
W2C_SUBTREES=("Geotechnical Documents/Boskalis OneDrive" "Geotechnical Documents/GWRM" "Geotechnical Documents/ICE Environmental Geotechnics" "Geotechnical Documents/TSP Training Information" "Geotechnical Documents/ARTC" "Geotechnical Documents/Proceedings" "Geotechnical Documents/ICE Forensic Engineering" "Geotechnical Documents/Woodside Engineering" "Geotechnical Documents/Hong Kong" "Geotechnical Documents/Conference papers Melbourne 2017" "Geotechnical Documents/Ground Anchors" "Geotechnical Documents/Performance Earthquake Conf" "Geotechnical Documents/ICE" "Geotechnical Documents/SLIDE" "Geotechnical Documents/Australian Geomechanics" "Geotechnical Documents/Metro Tunnel PPP-" "Geotechnical Documents/Rigid Inclusion" "Geotechnical Documents/EfW Design Documents" "Geotechnical Documents/Pway Institute" "Geotechnical Documents/Aspin Foundation Design Signal" "Geotechnical Documents/Earthquake Engineering" "Geotechnical Documents/BS6349" "Geotechnical Documents/Canada Piling" "Geotechnical Documents/Melb Metro GI" "Geotechnical Documents/LADS and ORBIS Guides, presentations, tutorials" "Geotechnical Documents/Pressuremeters GE" "Geotechnical Documents/AGS" "Geotechnical Documents/NCC_2016_Amdt1_All_Volumes" "Geotechnical Documents/Electranet" "Geotechnical Documents/DCP Technical Paper" "Geotechnical Documents/Oil Absorbing Geotextile" "Geotechnical Documents/Unicorn Drilling Parts" "Geotechnical Documents/PIGLET Training 2019" "Geotechnical Documents/Civil Engineering Reference Manual for the PE Exam, 15th Ed" "Geotechnical Documents/Design Engineering – November-December 2018" "Geotechnical Documents/HR" "Geotechnical Documents/Recent Challenges and Advances in Geotechnical Earthquake Engineering" "Geotechnical Documents/XVII European Conference on Soil Engineering")
W_BYL_SUBTREES=("Byland Technical")
W_BOOKS_SUBTREES=("CMW Projects" "Civil Engineering Books" "Geotechnique" "ICE Professional Review Docs" "Conferences" "M Bolton Publications" "Vancouver Geotechnical Society")

cmd() {
    local log="$1"; shift
    local line="nice -n 10 $PY $REPO/fastpass.py --root '$ROOT'"
    local s
    for s in "$@"; do line+=" --subtree '$ROOT/$s'"; done
    line+=" 2>&1 | tee -a $MD/$log"
    echo "$line"
}

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -n w1-tsp
for w in w2a-geo w2b-geo w2c-geo w3-byland w4-books w5-misc; do
    tmux new-window -d -t "$SESSION" -n "$w"
done
tmux send-keys -t "$SESSION:w1-tsp"    "$(cmd fastpass-tsp.log    "${W_TSP_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w2a-geo"   "$(cmd fastpass-w2a.log    "${W2A_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w2b-geo"   "$(cmd fastpass-w2b.log    "${W2B_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w2c-geo"   "$(cmd fastpass-w2c.log    "${W2C_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w3-byland" "$(cmd fastpass-byland.log "${W_BYL_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w4-books"  "$(cmd fastpass-books.log  "${W_BOOKS_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w5-misc"   "$(cmd fastpass-misc.log   "${W_MISC_SUBTREES[@]}")" Enter

echo "8 workers launched in tmux session '$SESSION'."
echo "  progress: wc -l < $MD/.fastpass.jsonl"
