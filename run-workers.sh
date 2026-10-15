#!/usr/bin/env bash
# run-workers.sh — 5 parallel fastpass workers (folder-disjoint, resume-safe).
#
# PAUSE : Ctrl-C inside each worker window — or just close the lid / reboot.
# RESUME: run this script again. Workers skip everything already in the manifest.
#
# OLEMI (32,375 drawing files) deliberately omitted 2026-10-14 — not wanted.
# 5-worker layout (2026-10-15): Geotechnical Documents split in 3 (it was the
# critical path at ~50% of remaining scope). Scale back to 3 by merging the
# w2* windows' subtrees if you need the CPU.

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

W1_SUBTREES=("TSP Projects" "OHW Standard Drawings" "RR Technical Info" "Uni Docs" "TSP & ARUP" "Nord Stream 2" "Technical Paper VE" "IStructE Exams" "Malcolm Drilling Technical Papers" "ICE Martime Journal" "NSW Proposals" "Pluit City" "Shirley Downloads" "Pipeline" "Job Description" "ICE Chartership")
W2A_SUBTREES=("Geotechnical Documents/Australian Geomechanics Journal" "Geotechnical Documents/3rd Conf Earthquake" "Geotechnical Documents/Tasmanian Government USB" "Geotechnical Documents/Australian Standards" "Geotechnical Documents/BHe HardDrive" "Geotechnical Documents/ICE Ground Improvement" "Geotechnical Documents/Victoria Basin Studies" "Geotechnical Documents/Humes Products" "Geotechnical Documents/Sheet Piling Drgs TSP" "Geotechnical Documents/CIRIA GUIDES" "Geotechnical Documents/West Gate Geotech" "Geotechnical Documents/CMW Review" "Geotechnical Documents/SLIDE" "Geotechnical Documents/Bamford Rock Testing" "Geotechnical Documents/CSW" "Geotechnical Documents/Rock Netting" "Geotechnical Documents/NSW" "Geotechnical Documents/Pway Institute" "Geotechnical Documents/EfW Design Documents" "Geotechnical Documents/Rock Embankment Technical Papers" "Geotechnical Documents/CRE Compentency" "Geotechnical Documents/Top ICE 10 Papers 2019" "Geotechnical Documents/Doherty Papers" "Geotechnical Documents/Pressuremeters GE" "Geotechnical Documents/Micropile Earth Retention" "Geotechnical Documents/ICE SPERW 3" "Geotechnical Documents/Melbourne East West Link" "Geotechnical Documents/AGS" "Geotechnical Documents/E & C Magazine" "Geotechnical Documents/Roger Bullivant Design" "Geotechnical Documents/Oil Absorbing Geotextile" "Geotechnical Documents/PIGLET Training 2019" "Geotechnical Documents/Foundation Drilling Magazine" "Geotechnical Documents/XVII European Conference on Soil Engineering" "Geotechnical Documents/Recent Challenges and Advances in Geotechnical Earthquake Engineering" "Geotechnical Documents/HR" "Geotechnical Documents/Design Engineering – November-December 2018" "Geotechnical Documents/Civil Engineering Reference Manual for the PE Exam, 15th Ed")
W2B_SUBTREES=("Geotechnical Documents/TSP Design Software and Information" "Geotechnical Documents/GRL Engineers" "Geotechnical Documents/ICE Geo Engineering Journal" "Geotechnical Documents/ICE Civil Engineering" "Geotechnical Documents/Geology Maps" "Geotechnical Documents/Road & Pavement Design" "Geotechnical Documents/International Journal of Geoengineering Case Histories" "Geotechnical Documents/IJCH Papers" "Geotechnical Documents/Ground Engineering Magazine" "Geotechnical Documents/PileBuck" "Geotechnical Documents/Rankine Lecture" "Geotechnical Documents/Performance Earthquake Conf" "Geotechnical Documents/Geofabrics" "Geotechnical Documents/Dredging Papers" "Geotechnical Documents/NCE" "Geotechnical Documents/Principles_of_Geotechnical_Engineering_8th_edition_Das_Sobhan_Solution_Manual" "Geotechnical Documents/Vibropile South Yarra" "Geotechnical Documents/AGEC" "Geotechnical Documents/Propex Erosion Information" "Geotechnical Documents/Ischebeck" "Geotechnical Documents/Aspin Foundation Design Signal" "Geotechnical Documents/Stab Sand" "Geotechnical Documents/Melb Metro GI" "Geotechnical Documents/Canada Piling" "Geotechnical Documents/Metro Wall" "Geotechnical Documents/GWEP ICE Proceedings" "Geotechnical Documents/GeoCongress 2024" "Geotechnical Documents/OHS Vic" "Geotechnical Documents/Horizontal directional Drilling" "Geotechnical Documents/Deflectometer" "Geotechnical Documents/Deep Vibratory Compaction" "Geotechnical Documents/Keynetix" "Geotechnical Documents/EFFC" "Geotechnical Documents/Well Licence" "Geotechnical Documents/Pile Cap - Tucker" "Geotechnical Documents/Engineering with Excel, 4th Edition" "Geotechnical Documents/Davis -  Geo Estructural" "Geotechnical Documents/Boskalis Spreadsheets")
W2C_SUBTREES=("Geotechnical Documents/Boskalis OneDrive" "Geotechnical Documents/GWRM" "Geotechnical Documents/ICE Environmental Geotechnics" "Geotechnical Documents/TSP Training Information" "Geotechnical Documents/ARTC" "Geotechnical Documents/Proceedings" "Geotechnical Documents/ICE Forensic Engineering" "Geotechnical Documents/Woodside Engineering" "Geotechnical Documents/Hong Kong" "Geotechnical Documents/Conference papers Melbourne 2017" "Geotechnical Documents/Ground Anchors" "Geotechnical Documents/ICE Technical Papers" "Geotechnical Documents/ICE" "Geotechnical Documents/Arup Geoguides" "Geotechnical Documents/Metro Tunnel PPP-" "Geotechnical Documents/Australian Geomechanics" "Geotechnical Documents/Rigid Inclusion" "Geotechnical Documents/Steel Piling Group Tech Notes" "Geotechnical Documents/Geotechnical Baseline Reports + Papers" "Geotechnical Documents/Rock Engineering" "Geotechnical Documents/Earthquake Engineering" "Geotechnical Documents/BS6349" "Geotechnical Documents/Example Metro Drawings" "Geotechnical Documents/Blue Johannah Road GI and Design" "Geotechnical Documents/LADS and ORBIS Guides, presentations, tutorials" "Geotechnical Documents/Elemex" "Geotechnical Documents/Point Load Tests" "Geotechnical Documents/NCC_2016_Amdt1_All_Volumes" "Geotechnical Documents/Electranet" "Geotechnical Documents/Unicorn Drilling Parts" "Geotechnical Documents/Pipe Jacking" "Geotechnical Documents/DCP Technical Paper" "Geotechnical Documents/CMW Ground Models" "Geotechnical Documents/Textbook of Engineering Drawing by K. Venkata Reddy" "Geotechnical Documents/Integrity Testing" "Geotechnical Documents/Engineering Design Representation and Reasoning" "Geotechnical Documents/Code of Practice for Project Management (Construction)" "Geotechnical Documents/03 CYP Procedures")
W3_SUBTREES=("Byland Technical" "CMW Projects" "Civil Engineering Books" "Geotechnique" "ICE Professional Review Docs" "Conferences" "M Bolton Publications" "Vancouver Geotechnical Society")

cmd() {
    local log="$1"; shift
    local line="nice -n 10 $PY $REPO/fastpass.py --root '$ROOT'"
    local s
    for s in "$@"; do line+=" --subtree '$ROOT/$s'"; done
    line+=" 2>&1 | tee -a $MD/$log"
    echo "$line"
}

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session  -d -s "$SESSION" -n w1-tsp
tmux new-window   -d -t "$SESSION" -n w2a-geo
tmux new-window   -d -t "$SESSION" -n w2b-geo
tmux new-window   -d -t "$SESSION" -n w2c-geo
tmux new-window   -d -t "$SESSION" -n w3-books
tmux send-keys -t "$SESSION:w1-tsp"   "$(cmd fastpass-w1.log "${W1_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w2a-geo"  "$(cmd fastpass-w2a.log "${W2A_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w2b-geo"  "$(cmd fastpass-w2b.log "${W2B_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w2c-geo"  "$(cmd fastpass-w2c.log "${W2C_SUBTREES[@]}")" Enter
tmux send-keys -t "$SESSION:w3-books" "$(cmd fastpass-w3.log "${W3_SUBTREES[@]}")" Enter

echo "5 workers launched in tmux session '$SESSION'."
echo "  progress: wc -l < $MD/.fastpass.jsonl"
