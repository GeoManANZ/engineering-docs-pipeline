#!/usr/bin/env bash
# vps-offload-round2.sh — second redistribution wave (2026-10-17).
# TSP Projects + 7 small Geotech subfolders -> VPS corpus. Then ensure the
# worker-loop is running (it may have self-exited between waves) and widened
# to 3 cores (taskset 0-2) now that hermes containers are up but idle.
set -euo pipefail

SRC="/mnt/c/Users/Chris/OneDrive/02. Work"
GEO="$SRC/Geotechnical Documents"

# top-level scope
echo "$(date '+%F %T') uploading: TSP Projects"
rsync -a "$SRC/TSP Projects" vps:'~/fastpass-vps/corpus/'

# Geotech subfolders (must land under corpus/"Geotechnical Documents"/ for rel-path fidelity)
for sub in "ICE Civil Engineering" "BHe HardDrive" "Road & Pavement Design" \
           "GRL Engineers" "ICE Ground Improvement" "Australian Geomechanics Journal" \
           "Victoria Basin Studies"; do
  echo "$(date '+%F %T') uploading: Geotechnical Documents/$sub"
  rsync -a "$GEO/$sub" vps:'~/fastpass-vps/corpus/Geotechnical Documents/'
done

echo "$(date '+%F %T') round-2 upload complete"
