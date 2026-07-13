#!/bin/bash
# Setup script for Engineering Docs Pipeline on G14 WSL2
# Run this once after cloning the repo.
set -e

echo "=========================================="
echo "Engineering Docs Pipeline v2 — Setup"
echo "=========================================="

# Check WSL2
if ! grep -qi microsoft /proc/version 2>/dev/null; then
    echo "⚠ Warning: This doesn't look like WSL2. The pipeline needs WSL2 + CUDA GPU."
fi

# Check CUDA
if command -v nvidia-smi &>/dev/null; then
    echo "✓ NVIDIA GPU detected:"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
else
    echo "✗ nvidia-smi not found. Install CUDA drivers for WSL2."
fi

# Check Java
if command -v java &>/dev/null; then
    JAVA_VER=$(java -version 2>&1 | head -1)
    echo "✓ Java: $JAVA_VER"
else
    echo "✗ Java not found. Install: sudo apt install openjdk-17-jdk"
fi

# Create venv
VENV_DIR="$HOME/pdf-convert-venv"
if [ -d "$VENV_DIR" ]; then
    echo "✓ Venv exists: $VENV_DIR"
else
    echo "Creating venv at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
    echo "✓ Venv created"
fi

# Activate and install
source "$VENV_DIR/bin/activate"
pip install --upgrade pip

echo "Installing opendataloader-pdf with hybrid extras..."
pip install "opendataloader-pdf[hybrid]"

echo "Installing MarkItDown (DOCX/XLSX converter)..."
pip install "markitdown[all]"

echo ""
echo "=========================================="
echo "Setup complete!"
echo "=========================================="
echo ""
echo "Create symlink for easy access:"
echo "  ln -s \"/mnt/c/Users/Chris/OneDrive/02. Work\" ~/work-docs"
echo ""
echo "Next steps:"
echo "  1. Open TWO terminals in WSL2:"
echo ""
echo "     Terminal 1 (hybrid server — leave running):"
echo "       cd ~/pdf-convert-venv && source bin/activate"
echo "       opendataloader-pdf-hybrid --port 5002 \\"
echo "         --force-ocr --ocr-engine rapidocr --device cuda \\"
echo "         --enrich-formula --enrich-picture-description"
echo ""
echo "     Terminal 2 (run pipeline):"
echo "       cd ~/engineering-docs-pipeline"
echo "       python3 pipeline.py --test    # 50-file test"
