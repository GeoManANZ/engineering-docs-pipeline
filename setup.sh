#!/bin/bash
set -e
echo "=========================================="
echo "Engineering Docs Pipeline v6 — Setup"
echo "=========================================="

if ! grep -qi microsoft /proc/version 2>/dev/null; then
    echo "⚠ Warning: This doesn't look like WSL2."
fi

if command -v nvidia-smi &>/dev/null; then
    echo "✓ NVIDIA GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader)"
else
    echo "✗ nvidia-smi not found."
fi

if command -v java &>/dev/null; then
    echo "✓ Java: $(java -version 2>&1 | head -1)"
else
    echo "✗ Java not found: sudo apt install openjdk-17-jdk"
fi

VENV_DIR="$HOME/pdf-convert-venv"
if [ -d "$VENV_DIR" ]; then
    echo "✓ Venv exists: $VENV_DIR"
else
    python3 -m venv "$VENV_DIR"
    echo "✓ Venv created"
fi

source "$VENV_DIR/bin/activate"
pip install --upgrade -q pip
pip install -q "opendataloader-pdf[hybrid]" "markitdown[all]" "pypdf"

echo ""
echo "=========================================="
echo "Setup complete!"
echo "=========================================="
echo ""
echo "Create symlink:"
echo "  ln -s \"/mnt/c/Users/Chris/OneDrive/02. Work\" ~/work-docs"
echo ""
echo "Three terminals needed:"
echo ""
echo "  Terminal 1 (digital PDFs — port 5002):"
echo "    cd ~/pdf-convert-venv && source bin/activate"
echo "    opendataloader-pdf-hybrid --port 5002 --device cuda --enrich-formula"
echo ""
echo "  Terminal 2 (scanned PDFs — port 5003):"
echo "    cd ~/pdf-convert-venv && source bin/activate"
echo "    opendataloader-pdf-hybrid --port 5003 --force-ocr --ocr-engine rapidocr --device cuda"
echo ""
echo "  Terminal 3 (pipeline):"
echo "    cd ~/pdf-convert-venv && source bin/activate"
echo "    cd ~/engineering-docs-pipeline && python3 pipeline.py --test"
