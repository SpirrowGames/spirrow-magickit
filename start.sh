#!/bin/bash
# Development startup script for Spirrow Magickit
# Usage: ./start.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source venv/bin/activate
export MAGICKIT_CONFIG="$SCRIPT_DIR/config/magickit_config.yaml"
exec python -m uvicorn magickit.main:app --host 0.0.0.0 --port 8113
