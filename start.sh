#!/bin/bash
# Development startup script for Spirrow Magickit
# Usage: ./start.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source venv/bin/activate
export MAGICKIT_CONFIG="$SCRIPT_DIR/config/magickit_config.yaml"
# loopback only: tailscale serve (:8443) proxies to 127.0.0.1:8113, so tailnet
# reach is unchanged. Binding 0.0.0.0 also exposed the dashboard to the LAN
# unauthenticated, and would let anyone who can reach the port forge the
# Tailscale-* identity headers that deploy approval now trusts -- see
# src/magickit/web/identity.py.
exec python -m uvicorn magickit.main:app --host 127.0.0.1 --port 8113
