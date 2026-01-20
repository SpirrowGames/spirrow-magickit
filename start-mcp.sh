#!/bin/bash
# Start Magickit MCP Server
#
# Usage: ./start-mcp.sh
#
# Environment variables:
#   MAGICKIT_MCP_PORT - Override MCP server port (default: 8114)
#   MAGICKIT_LOG_LEVEL - Override log level (default: INFO)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtual environment
if [ -d "venv" ]; then
    source venv/bin/activate
elif [ -d ".venv" ]; then
    source .venv/bin/activate
else
    echo "Error: No virtual environment found (venv or .venv)"
    exit 1
fi

# Ensure we're in the right directory for config loading
export PYTHONPATH="${SCRIPT_DIR}/src:${PYTHONPATH:-}"

echo "Starting Magickit MCP Server..."
echo "  Working directory: ${SCRIPT_DIR}"
echo "  Config: config/magickit_config.yaml"
echo "  Port: ${MAGICKIT_MCP_PORT:-8114}"

# Run the MCP server
exec python -m magickit.mcp_server
