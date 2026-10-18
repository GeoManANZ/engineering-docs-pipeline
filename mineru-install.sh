#!/usr/bin/env bash
# mineru-install.sh — GPU venv for Stage 2 on the RTX 5070 Ti (Blackwell sm_120:
# needs torch >= 2.7 cu128 wheels; plain pip torch would install CPU or old CUDA).
set -euo pipefail
V=~/mineru-venv
python3 -m venv "$V"
"$V/bin/pip" install -q --upgrade pip
"$V/bin/pip" install -q torch torchvision --index-url https://download.pytorch.org/whl/cu128
"$V/bin/pip" install -q "mineru[core]"
"$V/bin/python" - <<'PY'
import torch
print("torch:", torch.__version__)
print("cuda available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device:", torch.cuda.get_device_name(0))
    print("capability:", torch.cuda.get_device_capability(0))
PY
echo "INSTALL COMPLETE"
