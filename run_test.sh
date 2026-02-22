#!/usr/bin/env bash
set -euo pipefail

# Ensure Redis is running
if ! redis-cli ping > /dev/null 2>&1; then
    echo "ERROR: Redis is not running. Start it with: redis-server --daemonize yes"
    exit 1
fi

# Run the test script
exec python test.py
