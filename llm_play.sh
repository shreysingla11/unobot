#!/usr/bin/env bash
set -euo pipefail

# ── UNO: LLM (Player B) vs Automated Opponent (Player A) ──
#
# Usage:
#   ANTHROPIC_API_KEY=<key> ./llm_play.sh
#
# Requires: Redis running, Python venv with dependencies installed.

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY is not set."
    echo "Usage: ANTHROPIC_API_KEY=<key> ./llm_play.sh"
    exit 1
fi

if ! redis-cli ping > /dev/null 2>&1; then
    echo "ERROR: Redis is not running. Start it with: redis-server --daemonize yes"
    exit 1
fi

GAME_ID="llm_$(date +%s)"
echo "=== UNO: LLM vs Auto-Player | Game: $GAME_ID ==="
echo ""

# Clean up any stale state
redis-cli DEL "uno:${GAME_ID}" "uno:${GAME_ID}:lock" > /dev/null 2>&1

# Start automated Player A in background
python auto_player.py --game="$GAME_ID" --player=A &
PLAYER_A_PID=$!

cleanup() {
    kill "$PLAYER_A_PID" 2>/dev/null || true
    wait "$PLAYER_A_PID" 2>/dev/null || true
    redis-cli DEL "uno:${GAME_ID}" "uno:${GAME_ID}:lock" > /dev/null 2>&1
}
trap cleanup EXIT

echo "Auto Player A started (PID: $PLAYER_A_PID)"
sleep 2

# Run LLM as Player B
python python/chat.py --game-id="$GAME_ID"

echo ""
echo "=== Done ==="
