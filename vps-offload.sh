#!/usr/bin/env bash
# vps-offload.sh — one-shot: prep VPS env, upload the offloaded scopes, launch
# the VPS fastpass worker (steady slow lane). Safe to re-run: rsync is
# incremental; the VPS pipeline resumes from its own manifest.
set -euo pipefail

SRC="/mnt/c/Users/Chris/OneDrive/02. Work"
VPS=vps
BASE='~/fastpass-vps'

# 1. VPS environment (venv + deps, incremental)
ssh $VPS "python3 -m venv $BASE/venv 2>/dev/null || true; $BASE/venv/bin/pip install -q pymupdf pymupdf4llm 'markitdown[docx,xls]' && $BASE/venv/bin/python -c 'import pymupdf, pymupdf4llm; print(\"vps deps OK\")'"
scp -q ~/engineering-docs-pipeline/fastpass.py $VPS:$BASE/

# 2. Upload scopes (small first so conversion could start early if ever needed)
for scope in "Vancouver Geotechnical Society" "M Bolton Publications" "Geotechnique" \
             "Byland Technical" "Civil Engineering Books" "RR Technical Info" \
             "Conferences" "ICE Professional Review Docs"; do
  echo "$(date '+%F %T') uploading: $scope"
  rsync -a --stats "$SRC/$scope" "$VPS:$BASE/corpus/" 2>&1 | grep -E "total size|bytes/sec" | head -2
done

# 3. Launch the VPS pipeline (single nice worker, 2 of 4 cores, steady rate)
ssh $VPS "cd $BASE && pgrep -f 'fastpass.py' >/dev/null && echo 'vps worker already running' || (nohup nice -n 15 env OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 taskset -c 0-1 $BASE/venv/bin/python $BASE/fastpass.py --root $BASE/corpus --out $BASE/out > $BASE/pipeline.log 2>&1 & echo 'vps worker launched')"
echo "$(date '+%F %T') OFFLOAD COMPLETE — VPS pipeline running"
