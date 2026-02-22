# SOLUTION

## Overview

A complete MCP-based UNO game server supporting 2-4 players, with Redis-backed state, Pub/Sub turn coordination, LLM opponent integration, and per-player web dashboards.

## Architecture

```
main.py          — MCP stdio server + aiohttp web server (core game logic)
python/chat.py   — LLM player (Claude Haiku via mcp-use + LangChain)
auto_player.py   — Automated opponent (first-valid-card strategy)
```

**State management:** All game state lives in Redis as a single JSON blob keyed by `uno:{game_id}`. Each game's state includes the draw pile, discard pile, each player's hand, the current turn, active color, last action description, winner, player order, and play direction. This design makes the state fully serializable and allows any process to be killed and relaunched to resume a game from exactly where it left off.

**Concurrency control:** A Redis SETNX spin-lock (`uno:{game_id}:lock` with 5s TTL) serializes all writes. Every mutating operation (play, draw) acquires the lock, reads state, validates, mutates, saves, publishes a Pub/Sub notification, and releases — all atomically from the game's perspective.

**Turn notifications:** Redis Pub/Sub on channel `uno:{game_id}:turns` provides low-latency cross-process notifications. The `wait` tool subscribes *before* checking state (subscribe-before-check pattern) to avoid a race where a move happens between checking and subscribing.

## Parts Implemented

### Part 1: MCP Stdio Server (`main.py`)

The core `UnoGame` class manages all game state through Redis. It implements:

- **Deck building:** Standard 108-card UNO deck (number cards 0-9 in 4 colors, action cards Skip/Reverse/Draw Two in 4 colors, 4 Wilds, 4 Wild Draw Fours).
- **Game initialization:** Shuffle deck, deal 7 cards per player, flip first non-Wild card as starting discard. Starting card effects are applied (Skip/Reverse skip Player A, Draw Two makes A draw 2 and skip).
- **`status` tool:** Shows the player's hand (numbered), top discard card, current color, draw pile size, opponent card count(s), and whose turn it is or who won.
- **`play` tool:** Validates it's the player's turn, the card is in their hand, and the play is legal (matching color, matching number/type, or Wild). Applies action card effects: Skip keeps your turn, Reverse acts as Skip in 2p, Draw Two forces opponent to draw 2 and skips them, Wild Draw Four forces 4 draws and skips. Detects win (empty hand after play).
- **`draw` tool:** Draws one card from the draw pile, adds to hand, passes turn. Reshuffles discard pile (minus top card) back into draw pile when empty.
- **Card parsing:** `parse_card()` extracts color and type from card strings like `"Red 5"`, `"Green Skip"`, `"Wild Draw Four"`. `is_valid_play()` checks legality against top card and current color.

The server is implemented using the MCP SDK's `Server` class with `@server.list_tools()` and `@server.call_tool()` decorators, running over `stdio_server()`.

### Part 2: Test Script (`test.py`, `run_test.sh`)

The test script spawns two MCP server sub-processes using the MCP SDK's `stdio_client` and `ClientSession`, giving each a separate player identity. It exercises:

1. **Tool listing** — verifies all 4 tools (status, play, draw, wait) are registered
2. **Initial status** — both players see 7+ cards, consistent turn indicators (exactly one YOUR TURN, one OPPONENT'S TURN)
3. **Wrong-turn error** — the non-active player trying to draw gets an `isError=True` response with "not your turn"
4. **Invalid card error** — playing a card not in hand returns an error
5. **Full automated game** — plays up to 300 turns using a simple first-valid-card strategy, logging every move
6. **End-state validation** — player hands match Redis state, total cards == 108 (card conservation), winner has 0 cards, both players see consistent win/loss status

### Part 3: Wait Tool (`test_wait.py`, `run_wait_test.sh`)

The `wait` method on `UnoGame` uses Redis Pub/Sub for efficient blocking:

1. Subscribe to the game's Pub/Sub channel
2. After subscribing, check if it's already our turn or game is over — if so, return immediately (this ordering prevents the race condition where a move happens between check and subscribe)
3. Otherwise, loop waiting for Pub/Sub messages with a configurable timeout
4. On each message, re-read state from Redis and check if it's our turn
5. Clean up subscription in a `finally` block

The `play()` and `draw()` methods publish an `"update"` message after every state save, still within the lock, ensuring notifications only fire after state is persisted.

The test script validates:
- **Immediate return** when it's already the player's turn
- **Concurrent blocking/unblocking** — starts a `wait` call, then makes the opponent move after a delay, verifying wait returns with the correct last action (Test 2b)
- **Full wait-coordinated game** — alternating wait/play between players for a complete game
- **Post-game wait** — wait returns immediately after game over

### Part 4: LLM Player (`python/chat.py`, `auto_player.py`, `llm_play.sh`)

`chat.py` connects Claude Haiku (via `langchain-anthropic` and `mcp-use` MCPAgent) to the UNO MCP server as Player B. The system prompt teaches the LLM:

- UNO rules and card effects
- The exact tool-calling sequence: `wait` -> `status` -> `play`/`draw`
- Card naming conventions (exact match required, e.g. "Red Draw Two" not "Red draw two")
- Color choice strategy (pick the color you have the most of)
- Game-over detection

The main loop issues turn-by-turn prompts, streaming tool calls and LLM responses. Game-over is detected from tool observations (`"won!"`, `"game is already over"`) and from the LLM's text response. A fallback stops the game if the LLM goes 3 turns without calling any tools.

`auto_player.py` provides a deterministic automated opponent using the same MCP client pattern. It connects to the server, loops on `wait` -> `status` -> first-valid-card `play`/`draw`, and handles multi-player status formats.

`llm_play.sh` orchestrates a game: cleans Redis state, starts `auto_player.py` as Player A in the background, then runs `chat.py` as Player B.

### Part 5: Multi-Player + Web Server

The game state was extended with two new fields:
- `player_order`: list of player IDs in seating order (e.g. `["A","B","C"]`)
- `direction`: `1` for clockwise, `-1` for counter-clockwise

The `_next_player(state, from_player, skip=1)` helper computes the next player using modular arithmetic: `order[(idx + direction * skip) % len(order)]`. Python's modulo always returns non-negative for positive divisors, so counter-clockwise wrapping works correctly.

**Card effects in 3+ players:**

| Card | 2 Players | 3+ Players |
|------|-----------|------------|
| Skip | Keep turn (skip opponent) | Skip next player, turn to player after |
| Reverse | Keep turn (= Skip) | Flip direction, next player in new direction |
| Draw Two | Opponent draws 2, keep turn | Next player draws 2 and is skipped |
| Wild Draw Four | Opponent draws 4, keep turn | Next player draws 4 and is skipped |

**Status output** adapts to player count: 2-player games show `"Opponent has: N cards"` and `"OPPONENT'S TURN"` (backward-compatible), while 3+ player games show `"Player B has: N cards"`, `"Player C's TURN"`, and a `"Direction: Clockwise/Counter-clockwise"` indicator.

**Web server:** Each player process starts an `aiohttp` web server on `localhost:19000+offset` (A=19000, B=19001, etc.) serving an auto-refreshing HTML page with the player's current game status. The web server runs concurrently with the MCP stdio server on the same asyncio event loop.

**Backward compatibility:** `--num-players` defaults to 2, so all existing 2-player commands and tests work unchanged. `get_state()` includes migration logic that adds `player_order=["A","B"]` and `direction=1` to old game states missing these fields.

## How to Run

**Prerequisites:** Python 3.11+, Redis running locally.

```bash
# Install dependencies
pip install -e . mcp-use redis aiohttp

# Ensure Redis is running
redis-server --daemonize yes
```

### Run Tests

```bash
bash run_test.sh              # Part 2: 2-player full game test
bash run_wait_test.sh         # Part 3: wait tool + Pub/Sub test
bash run_multiplayer_test.sh  # Part 5: 3-player & 4-player tests
python test_regression.py     # 13 targeted 2-player regression tests
```

### Run LLM Player

```bash
ANTHROPIC_API_KEY=<key> bash llm_play.sh
```

### Manual Multi-Player Game

```bash
redis-cli DEL uno:mp3 uno:mp3:lock
python main.py --game=mp3 --player=A --num-players=3 &
python main.py --game=mp3 --player=B --num-players=3 &
python main.py --game=mp3 --player=C --num-players=3 &
# Web dashboards at http://localhost:19000/ 19001/ 19002/
```

## Test Summary

| Script | What it tests |
|--------|--------------|
| `run_test.sh` | 2-player: tool listing, status, play, draw, errors, full game, card conservation |
| `run_wait_test.sh` | Wait tool: immediate return, concurrent blocking, wait-coordinated game |
| `run_multiplayer_test.sh` | 3/4-player: Reverse direction change, Skip skips correct player, Draw Two victim draws and is skipped, full 3p and 4p games to completion, web server response on all ports |
| `test_regression.py` | 13 targeted tests: Skip/Reverse/Draw Two/Wild Draw Four/Wild/Number in 2p, draw turn passing, status format preservation, old state migration, web server alongside MCP, wait tool, win detection + game-over, 3 randomized full games with card conservation |

## AI Tool Usage

Development used a mix of Claude Code (Claude Opus 4.6) and manual editing. Claude Code was used for initial codebase exploration, planning the approach for each part, generating implementation code, and running test suites iteratively to diagnose and fix failures. Manual work included reviewing generated plans, making targeted edits (e.g. redis client API preferences, additional concurrency tests in `test_wait.py`), and directing the overall development flow. The MCP SDK documentation at `github.com/modelcontextprotocol/python-sdk` was referenced for server/client API patterns.
