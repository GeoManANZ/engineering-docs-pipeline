#!/usr/bin/env bash
# hydration-watch.sh — wait for OneDrive to finish downloading the pinned
# corpus, then relaunch the fastpass workers. Exits (and notifies) when done.
#
# Probe: cloud-only count (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, 0x400000)
# over the three bulk folders every 10 min, then a full-scope confirmation
# pass. Three consecutive unchanged probes = stalled sync -> exit with warning
# (check the OneDrive tray icon for paused sync / sign-in prompts).

BASE='/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
declare -a SCOPES=("Geotechnical Documents" "TSP Projects" "Byland Technical"
  "OHW Standard Drawings" "RR Technical Info" "Uni Docs" "TSP & ARUP"
  "Nord Stream 2" "Technical Paper VE" "IStructE Exams"
  "Malcolm Drilling Technical Papers" "ICE Martime Journal" "NSW Proposals"
  "Pluit City" "Shirley Downloads" "Pipeline" "Job Description"
  "ICE Chartership" "CMW Projects" "Civil Engineering Books" "Geotechnique"
  "ICE Professional Review Docs" "Conferences" "M Bolton Publications"
  "Vancouver Geotechnical Society")

probe() {  # probe <folder...> -> cloud-only count
    local dirs="$1"; shift
    local ps="\$b='C:\Users\Chris\OneDrive\02. Work'; \$t=0;"
    local d
    for d in "$@"; do
        ps+=" Get-ChildItem -LiteralPath \"\$b\\$d\" -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { if (\$_.Attributes -band 0x400000) {\$t++} };"
    done
    ps+=" \$t"
    "$BASE" -NoProfile -Command "$ps" 2>/dev/null | tr -d '\r\n'
}

last=-1; same=0
while true; do
    heavy=$(probe x "Geotechnical Documents" "TSP Projects" "Byland Technical")
    echo "$(date '+%F %T') heavy-folder cloud-only: ${heavy:-n/a}"
    if [ "${heavy:-x}" = "0" ]; then
        full=$(probe x "${SCOPES[@]}")
        echo "$(date '+%F %T') full-scope cloud-only: ${full:-n/a}"
        if [ "${full:-x}" = "0" ]; then
            echo "$(date '+%F %T') HYDRATION COMPLETE — relaunching workers"
            bash "$HOME/engineering-docs-pipeline/run-workers.sh" \
                && exit 0 || exit 1
        fi
    fi
    if [ "$heavy" = "$last" ]; then same=$((same+1)); else same=0; fi
    if [ "$same" -ge 3 ]; then
        echo "$(date '+%F %T') STALLED at $heavy cloud-only for 30+ min — check the OneDrive tray icon (paused sync? sign-in? quota?)."
        exit 2
    fi
    last=$heavy
    sleep 600
done
