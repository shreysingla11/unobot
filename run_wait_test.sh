#!/usr/bin/env bash
set -euo pipefail
if ! redis-cli ping > /dev/null 2>&1; then
    echo "ERROR: Redis is not running."
    exit 1
fi
exec python test_wait.py
