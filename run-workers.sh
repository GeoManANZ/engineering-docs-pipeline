#!/usr/bin/env bash
# run-workers.sh — 10 parallel fastpass workers (folder-disjoint, resume-safe).
#
# PAUSE : Ctrl-C in any worker window — or lid-close / reboot. Nothing is lost;
#         the manifest fsyncs per file. RESUME: run this script again.
#
# Layout (2026-10-15, 18 vCPUs): balanced on REMAINING retry counts, not totals.
# w06 uses --root-files: loose PDFs directly in the Geotechnical Documents root
# that subtree walkers never reach. OLEMI remains excluded by user choice.

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

W01_TSP_SUBTREES=("TSP Projects")
W02_GEOA_SUBTREES=("Geotechnical Documents/GWRM" "Geotechnical Documents/3rd Conf Earthquake" "Geotechnical Documents/Tasmanian Government USB" "Geotechnical Documents/ICE Environmental Geotechnics")
W03_GEOB_SUBTREES=("Geotechnical Documents/Boskalis OneDrive" "Geotechnical Documents/ICE Geo Engineering Journal" "Geotechnical Documents/TSP Training Information" "Geotechnical Documents/Australian Geomechanics Journal")
W04_GEOC_SUBTREES=("Geotechnical Documents/Australian Standards" "Geotechnical Documents/Geology Maps" "Geotechnical Documents/ARTC" "Geotechnical Documents/BHe HardDrive" "Geotechnical Documents/Road & Pavement Design" "Geotechnical Documents/Proceedings" "Geotechnical Documents/ICE Civil Engineering")
W05_GEOD_SUBTREES=("Geotechnical Documents/03 CYP Procedures" "Geotechnical Documents/101 Things I Learned in Engineering School  by John Kuprenas and Matthew Frederick" "Geotechnical Documents/AGEC" "Geotechnical Documents/AGS" "Geotechnical Documents/AMCRPS_dwg_Files_2019_03" "Geotechnical Documents/Advances in Numerical Methods in Geotechnical Engineering" "Geotechnical Documents/Arup Geoguides" "Geotechnical Documents/Aspin Foundation Design Signal" "Geotechnical Documents/Australian Geomechanics" "Geotechnical Documents/BS6349" "Geotechnical Documents/Bamford Rock Testing" "Geotechnical Documents/Blue Johannah Road GI and Design" "Geotechnical Documents/Boskalis Spreadsheets" "Geotechnical Documents/CIRIA GUIDES" "Geotechnical Documents/CMW Ground Models" "Geotechnical Documents/CMW Presentations" "Geotechnical Documents/CMW Review" "Geotechnical Documents/CRE Compentency" "Geotechnical Documents/CSW" "Geotechnical Documents/Canada Piling" "Geotechnical Documents/Civil Engineering Reference Manual for the PE Exam, 15th Ed" "Geotechnical Documents/Code of Practice for Project Management (Construction)" "Geotechnical Documents/Conference papers Melbourne 2017" "Geotechnical Documents/Contract Law For Dummies - Scott J. Burnham" "Geotechnical Documents/DCP Technical Paper" "Geotechnical Documents/Davis -  Geo Estructural" "Geotechnical Documents/Deep Vibratory Compaction" "Geotechnical Documents/Deflectometer" "Geotechnical Documents/Design Engineering – November-December 2018" "Geotechnical Documents/Doherty Papers" "Geotechnical Documents/Dredging Papers" "Geotechnical Documents/E & C Magazine" "Geotechnical Documents/EFFC" "Geotechnical Documents/Earthquake Engineering" "Geotechnical Documents/EfW Design Documents" "Geotechnical Documents/Electranet" "Geotechnical Documents/Elemex" "Geotechnical Documents/Engineering Design Representation and Reasoning" "Geotechnical Documents/Engineering with Excel, 4th Edition" "Geotechnical Documents/Example Metro Drawings" "Geotechnical Documents/Foundation Drilling Magazine" "Geotechnical Documents/GRL Engineers" "Geotechnical Documents/GWEP ICE Proceedings" "Geotechnical Documents/GWRM Photos" "Geotechnical Documents/GeoCongress 2024" "Geotechnical Documents/Geofabrics" "Geotechnical Documents/Geology For Dummies  by Alecia M. Spooner. EPUB (Perseu)" "Geotechnical Documents/Geotechnical Baseline Reports + Papers" "Geotechnical Documents/Ground Anchors" "Geotechnical Documents/Ground Engineering Magazine" "Geotechnical Documents/HR" "Geotechnical Documents/Handbook of Structural Engineering" "Geotechnical Documents/Hong Kong" "Geotechnical Documents/Horizontal directional Drilling" "Geotechnical Documents/Humes Products" "Geotechnical Documents/ICE" "Geotechnical Documents/ICE Forensic Engineering" "Geotechnical Documents/ICE Ground Improvement" "Geotechnical Documents/ICE SPERW 3" "Geotechnical Documents/ICE Technical Papers" "Geotechnical Documents/IJCH Papers" "Geotechnical Documents/Integrity Testing" "Geotechnical Documents/International Journal of Geoengineering Case Histories" "Geotechnical Documents/Ischebeck" "Geotechnical Documents/Keynetix" "Geotechnical Documents/LADS and ORBIS Guides, presentations, tutorials" "Geotechnical Documents/Melb Metro GI" "Geotechnical Documents/Melbourne East West Link" "Geotechnical Documents/Metro Tunnel PPP-" "Geotechnical Documents/Metro Wall" "Geotechnical Documents/Micropile Earth Retention" "Geotechnical Documents/NCC_2016_Amdt1_All_Volumes" "Geotechnical Documents/NCE" "Geotechnical Documents/NS2 Presentation" "Geotechnical Documents/NSW" "Geotechnical Documents/OHS Vic" "Geotechnical Documents/OLE-GEO Training Package" "Geotechnical Documents/Oil Absorbing Geotextile" "Geotechnical Documents/PIGLET Training 2019" "Geotechnical Documents/Performance Earthquake Conf" "Geotechnical Documents/Picture of Lateral Pile Test" "Geotechnical Documents/Pile Cap - Tucker" "Geotechnical Documents/PileBuck" "Geotechnical Documents/Pipe Jacking" "Geotechnical Documents/Point Load Tests" "Geotechnical Documents/Pressuremeters GE" "Geotechnical Documents/Principles_of_Geotechnical_Engineering_8th_edition_Das_Sobhan_Solution_Manual" "Geotechnical Documents/Propex Erosion Information" "Geotechnical Documents/Pway Institute" "Geotechnical Documents/Rankine Lecture" "Geotechnical Documents/Recent Challenges and Advances in Geotechnical Earthquake Engineering" "Geotechnical Documents/Rigid Inclusion" "Geotechnical Documents/Rock Embankment Technical Papers" "Geotechnical Documents/Rock Engineering" "Geotechnical Documents/Rock Netting" "Geotechnical Documents/Roger Bullivant Design" "Geotechnical Documents/SLIDE" "Geotechnical Documents/Sheet Piling Drgs TSP" "Geotechnical Documents/Stab Sand" "Geotechnical Documents/Steel Piling Group Tech Notes" "Geotechnical Documents/TSP Design Software and Information" "Geotechnical Documents/Textbook of Engineering Drawing by K. Venkata Reddy" "Geotechnical Documents/The Engineering Book - From the Catapult to the Curiosity Rover, 250 Milestones in the History of Engineering (Sterling Milestones)" "Geotechnical Documents/Top ICE 10 Papers 2019" "Geotechnical Documents/Unicorn Drilling Parts" "Geotechnical Documents/Vibropile South Yarra" "Geotechnical Documents/Victoria Basin Studies" "Geotechnical Documents/Well Licence" "Geotechnical Documents/West Gate Geotech" "Geotechnical Documents/Woodside Engineering" "Geotechnical Documents/XVII European Conference on Soil Engineering")
W06_GEOLOOSE_SUBTREES=("Geotechnical Documents")
W07_BYLAND_SUBTREES=("Byland Technical" "CMW Projects" "M Bolton Publications" "Vancouver Geotechnical Society")
W08_BOOKS_SUBTREES=("Civil Engineering Books" "Geotechnique" "ICE Professional Review Docs" "Conferences")
W09_RR_SUBTREES=("RR Technical Info" "Uni Docs" "TSP & ARUP" "Nord Stream 2" "Technical Paper VE" "IStructE Exams" "Malcolm Drilling Technical Papers" "ICE Martime Journal" "NSW Proposals" "Pluit City" "Shirley Downloads" "Pipeline" "Job Description" "ICE Chartership" "Boskalis Computer")
W10_OHW_SUBTREES=("OHW Standard Drawings" "Aus Chartership" "Australian Tunnelling" "Brian Perry Civils")

cmd() {
    local log="$1"; shift
    local line="nice -n 10 $PY $REPO/fastpass.py --root '$ROOT'"
    local s
    for s in "$@"; do line+=" --subtree '$ROOT/$s'"; done
    line+=" 2>&1 | tee -a $MD/$log"
    echo "$line"
}

cmdrf() {  # --root-files variant: loose files directly in DIR
    local log="$1"; shift
    local line="nice -n 10 $PY $REPO/fastpass.py --root '$ROOT'"
    local s
    for s in "$@"; do line+=" --root-files '$ROOT/$s'"; done
    line+=" 2>&1 | tee -a $MD/$log"
    echo "$line"
}

tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION"
tmux rename-window -t "$SESSION" w01-tsp
tmux send-keys -t "$SESSION:w01-tsp" "$(cmd fastpass-w01-tsp.log "${W01_TSP_SUBTREES[@]}")" Enter
tmux new-window -d -t "$SESSION" -n w02-geoA
tmux send-keys -t "$SESSION:w02-geoA" "$(cmd fastpass-w02-geoA.log "${W02_GEOA_SUBTREES[@]}")" Enter
tmux new-window -d -t "$SESSION" -n w03-geoB
tmux send-keys -t "$SESSION:w03-geoB" "$(cmd fastpass-w03-geoB.log "${W03_GEOB_SUBTREES[@]}")" Enter
tmux new-window -d -t "$SESSION" -n w04-geoC
tmux send-keys -t "$SESSION:w04-geoC" "$(cmd fastpass-w04-geoC.log "${W04_GEOC_SUBTREES[@]}")" Enter
tmux new-window -d -t "$SESSION" -n w05-geoD
tmux send-keys -t "$SESSION:w05-geoD" "bash $REPO/workers/w05-geoD.sh" Enter
tmux new-window -d -t "$SESSION" -n w06-geoloose
tmux send-keys -t "$SESSION:w06-geoloose" "$(cmdrf fastpass-w06-geoloose.log "${W06_GEOLOOSE_SUBTREES[@]}")" Enter
tmux new-window -d -t "$SESSION" -n w07-byland
tmux send-keys -t "$SESSION:w07-byland" "$(cmd fastpass-w07-byland.log "${W07_BYLAND_SUBTREES[@]}")" Enter
tmux new-window -d -t "$SESSION" -n w08-books
tmux send-keys -t "$SESSION:w08-books" "$(cmd fastpass-w08-books.log "${W08_BOOKS_SUBTREES[@]}")" Enter
tmux new-window -d -t "$SESSION" -n w09-rr
tmux send-keys -t "$SESSION:w09-rr" "$(cmd fastpass-w09-rr.log "${W09_RR_SUBTREES[@]}")" Enter
tmux new-window -d -t "$SESSION" -n w10-ohw
tmux send-keys -t "$SESSION:w10-ohw" "$(cmd fastpass-w10-ohw.log "${W10_OHW_SUBTREES[@]}")" Enter

echo "10 workers launched in tmux session '$SESSION'."
echo "  progress: wc -l < $MD/.fastpass.jsonl"
