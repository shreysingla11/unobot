#!/usr/bin/env bash
set -euo pipefail

if ! redis-cli ping > /dev/null 2>&1; then
    echo "ERROR: Redis is not running. Start it with: redis-server --daemonize yes"
    exit 1
fi

redis-cli FLUSHDB > /dev/null
python test_multiplayer.py
echo "All multi-player tests passed!"
