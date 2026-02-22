#!/usr/bin/env python3
"""
Thorough regression test for parts 1-4 after the multi-player refactor.

Validates:
- 2-player Skip/Reverse/Draw Two/Wild Draw Four/Wild/Number all behave identically
- Old game state migration (missing player_order/direction)
- Status output format is exactly preserved for 2-player
- Turn cycling is correct
- Card conservation after every action card
- Web server is reachable and doesn't corrupt MCP

Usage:
    python test_regression.py
"""

import asyncio
import json
import os
import random
import sys
import uuid

import aiohttp
import redis.asyncio as aioredis
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PYTHON = sys.executable
MAIN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
COLORS = ["Red", "Yellow", "Green", "Blue"]
PORT_MAP = {"A": 19000, "B": 19001}


def log(msg: str) -> None:
    print(msg, flush=True)


def extract_text(result) -> str:
    return result.content[0].text


def parse_hand_from_status(status_text: str) -> list[str]:
    cards = []
    in_hand = False
    for line in status_text.splitlines():
        if line.strip() == "=== Your Hand ===":
            in_hand = True
            continue
        if line.strip() == "" and in_hand:
            break
        if in_hand:
            parts = line.strip().split(". ", 1)
            if len(parts) == 2:
                cards.append(parts[1])
    return cards


def parse_status_line(status_text: str) -> str:
    for line in status_text.splitlines():
        if line.startswith("Status: "):
            return line[len("Status: "):]
    return ""


class MCPPlayer:
    def __init__(self, name: str):
        self.name = name
        self._stdio_cm = None
        self._session_cm = None
        self.session: ClientSession | None = None

    async def start(self, game_id: str, player: str) -> None:
        params = StdioServerParameters(
            command=PYTHON,
            args=[MAIN_PY, f"--game={game_id}", f"--player={player}"],
        )
        self._stdio_cm = stdio_client(params)
        read_stream, write_stream = await self._stdio_cm.__aenter__()
        self._session_cm = ClientSession(read_stream, write_stream)
        self.session = await self._session_cm.__aenter__()
        await self.session.initialize()

    async def stop(self) -> None:
        if self._session_cm:
            await self._session_cm.__aexit__(None, None, None)
        if self._stdio_cm:
            await self._stdio_cm.__aexit__(None, None, None)

    async def call(self, tool: str, arguments: dict | None = None) -> tuple[str, bool]:
        result = await self.session.call_tool(tool, arguments or {})
        text = extract_text(result)
        is_err = getattr(result, "isError", False)
        return text, is_err


async def count_total_cards(r, game_id, player_ids=("A", "B")):
    """Return total cards in the game."""
    raw = await r.get(f"uno:{game_id}")
    state = json.loads(raw)
    total = sum(len(state["hands"][p]) for p in player_ids)
    total += len(state["draw_pile"]) + len(state["discard_pile"])
    return total


async def verify_card_conservation(r, game_id, expected, player_ids=("A", "B")):
    """Verify total cards equals expected value."""
    total = await count_total_cards(r, game_id, player_ids)
    assert total == expected, f"Card conservation violated: {total} != {expected}"


# ---------------------------------------------------------------------------
# Test 1: Controlled 2-player game — Skip card
# ---------------------------------------------------------------------------
async def test_skip_2p():
    log("\n--- Test: Skip in 2-player ---")
    game_id = f"reg_skip_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    deck = [f"Red {i}" for i in range(10)] * 10
    state = {
        "draw_pile": deck[:80],
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Red Skip", "Blue 3", "Green 7", "Yellow 1", "Red 2", "Blue 8", "Green 4"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        # Player A plays Skip → keeps turn (B is skipped)
        text, err = await pa.call("play", {"card": "Red Skip"})
        assert not err, f"Error: {text}"
        assert "B is skipped" in text, f"Expected 'B is skipped' in: {text}"

        raw = await r.get(f"uno:{game_id}")
        s = json.loads(raw)
        assert s["current_turn"] == "A", f"After Skip, should be A's turn, got {s['current_turn']}"
        assert len(s["hands"]["A"]) == 6, "A should have 6 cards after playing Skip"

        await verify_card_conservation(r, game_id, expected=95)
        log("  PASS: Skip gives A another turn, B skipped.")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 2: Controlled 2-player game — Reverse card (acts as Skip in 2p)
# ---------------------------------------------------------------------------
async def test_reverse_2p():
    log("\n--- Test: Reverse in 2-player (should act as Skip) ---")
    game_id = f"reg_rev_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    state = {
        "draw_pile": [f"Red {i}" for i in range(10)] * 8,
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Red Reverse", "Blue 3", "Green 7", "Yellow 1", "Red 2", "Blue 8", "Green 4"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        text, err = await pa.call("play", {"card": "Red Reverse"})
        assert not err, f"Error: {text}"
        assert "B is skipped" in text, f"Expected 'B is skipped' in: {text}"

        raw = await r.get(f"uno:{game_id}")
        s = json.loads(raw)
        assert s["current_turn"] == "A", f"After Reverse (2p), should be A's turn, got {s['current_turn']}"
        # Direction should NOT change in 2-player Reverse
        assert s["direction"] == 1, f"Direction should stay 1 in 2p Reverse, got {s['direction']}"

        await verify_card_conservation(r, game_id, expected=95)
        log("  PASS: Reverse acts as Skip in 2-player, direction unchanged.")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 3: Draw Two in 2-player
# ---------------------------------------------------------------------------
async def test_draw_two_2p():
    log("\n--- Test: Draw Two in 2-player ---")
    game_id = f"reg_d2_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    state = {
        "draw_pile": [f"Yellow {i}" for i in range(10)] * 8,
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Red Draw Two", "Blue 3", "Green 7", "Yellow 1", "Red 2", "Blue 8", "Green 4"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        text, err = await pa.call("play", {"card": "Red Draw Two"})
        assert not err, f"Error: {text}"
        assert "B draws 2 and is skipped" in text, f"Unexpected: {text}"

        raw = await r.get(f"uno:{game_id}")
        s = json.loads(raw)
        assert s["current_turn"] == "A", f"After Draw Two (2p), should be A's turn, got {s['current_turn']}"
        assert len(s["hands"]["B"]) == 9, f"B should have 9 cards (7+2), got {len(s['hands']['B'])}"
        assert len(s["hands"]["A"]) == 6, f"A should have 6 cards, got {len(s['hands']['A'])}"

        await verify_card_conservation(r, game_id, expected=95)
        log("  PASS: Draw Two — B draws 2, B is skipped, A keeps turn.")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 4: Wild Draw Four in 2-player
# ---------------------------------------------------------------------------
async def test_wild_draw_four_2p():
    log("\n--- Test: Wild Draw Four in 2-player ---")
    game_id = f"reg_wd4_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    state = {
        "draw_pile": [f"Green {i}" for i in range(10)] * 8,
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Wild Draw Four", "Blue 3", "Green 7", "Yellow 1", "Red 2", "Blue 8", "Green 4"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        text, err = await pa.call("play", {"card": "Wild Draw Four", "chosen_color": "Blue"})
        assert not err, f"Error: {text}"
        assert "B draws 4 and is skipped" in text, f"Unexpected: {text}"
        assert "Color is now Blue" in text, f"Expected color change in: {text}"

        raw = await r.get(f"uno:{game_id}")
        s = json.loads(raw)
        assert s["current_turn"] == "A", f"After WD4 (2p), should be A's turn, got {s['current_turn']}"
        assert len(s["hands"]["B"]) == 11, f"B should have 11 cards (7+4), got {len(s['hands']['B'])}"
        assert s["current_color"] == "Blue", f"Color should be Blue, got {s['current_color']}"

        await verify_card_conservation(r, game_id, expected=95)
        log("  PASS: Wild Draw Four — B draws 4, B skipped, color changed.")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 5: Wild card (no draw) in 2-player
# ---------------------------------------------------------------------------
async def test_wild_2p():
    log("\n--- Test: Wild card in 2-player ---")
    game_id = f"reg_w_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    state = {
        "draw_pile": [f"Green {i}" for i in range(10)] * 8,
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Wild", "Blue 3", "Green 7", "Yellow 1", "Red 2", "Blue 8", "Green 4"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        text, err = await pa.call("play", {"card": "Wild", "chosen_color": "Green"})
        assert not err, f"Error: {text}"
        assert "Color is now Green" in text, f"Expected color change in: {text}"

        raw = await r.get(f"uno:{game_id}")
        s = json.loads(raw)
        # Wild does NOT skip — turn passes to opponent
        assert s["current_turn"] == "B", f"After Wild (2p), should be B's turn, got {s['current_turn']}"
        assert s["current_color"] == "Green", f"Color should be Green, got {s['current_color']}"

        await verify_card_conservation(r, game_id, expected=95)
        log("  PASS: Wild — turn passes to B, color changed.")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 6: Normal number card in 2-player
# ---------------------------------------------------------------------------
async def test_number_2p():
    log("\n--- Test: Number card in 2-player ---")
    game_id = f"reg_num_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    state = {
        "draw_pile": [f"Green {i}" for i in range(10)] * 8,
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Red 3", "Blue 3", "Green 7", "Yellow 1", "Red 2", "Blue 8", "Green 4"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        text, err = await pa.call("play", {"card": "Red 3"})
        assert not err, f"Error: {text}"
        assert text == "You played Red 3.", f"Unexpected message: {text}"

        raw = await r.get(f"uno:{game_id}")
        s = json.loads(raw)
        assert s["current_turn"] == "B", f"After number card, should be B's turn, got {s['current_turn']}"

        await verify_card_conservation(r, game_id, expected=95)
        log("  PASS: Number card — turn passes to B.")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 7: Draw card in 2-player
# ---------------------------------------------------------------------------
async def test_draw_2p():
    log("\n--- Test: Draw in 2-player ---")
    game_id = f"reg_draw_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    state = {
        "draw_pile": [f"Green {i}" for i in range(10)] * 7 + ["Yellow 9"],
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Blue 3", "Green 7", "Yellow 1", "Blue 8", "Green 4", "Blue 6", "Green 0"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        text, err = await pa.call("draw")
        assert not err, f"Error: {text}"
        assert "You drew: Yellow 9" in text, f"Unexpected: {text}"

        raw = await r.get(f"uno:{game_id}")
        s = json.loads(raw)
        assert s["current_turn"] == "B", f"After draw, should be B's turn, got {s['current_turn']}"
        assert len(s["hands"]["A"]) == 8, f"A should have 8 cards (7+1), got {len(s['hands']['A'])}"

        log("  PASS: Draw — turn passes to B, A gets 1 card.")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 8: Status format is exactly correct for 2-player
# ---------------------------------------------------------------------------
async def test_status_format_2p():
    log("\n--- Test: Status output format for 2-player ---")
    game_id = f"reg_fmt_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    state = {
        "draw_pile": [f"Green {i}" for i in range(10)] * 8,
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Red 3", "Blue 3"],
            "B": ["Red 1", "Blue 2", "Green 3"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        # Check A's status
        text_a, _ = await pa.call("status")
        lines_a = text_a.splitlines()
        assert "Opponent has: 3 cards" in lines_a, f"Expected 'Opponent has: 3 cards', got: {lines_a}"
        assert "Status: YOUR TURN" in lines_a, f"Expected YOUR TURN in A's status"
        # No "Direction" line for 2-player
        assert not any("Direction:" in l for l in lines_a), "Should NOT have Direction line in 2-player"
        # No "Player X has" format in 2-player
        assert not any(l.startswith("Player ") and "has:" in l for l in lines_a), \
            "Should use 'Opponent has:' not 'Player X has:' in 2-player"

        # Check B's status
        text_b, _ = await pb.call("status")
        lines_b = text_b.splitlines()
        assert "Opponent has: 2 cards" in lines_b, f"Expected 'Opponent has: 2 cards', got: {lines_b}"
        assert "Status: OPPONENT'S TURN" in lines_b, f"Expected OPPONENT'S TURN in B's status"

        log("  PASS: 2-player status format is correct (Opponent, no Direction).")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 9: Old game state migration (missing player_order/direction)
# ---------------------------------------------------------------------------
async def test_old_state_migration():
    log("\n--- Test: Old game state migration ---")
    game_id = f"reg_old_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    # Simulate an old game state WITHOUT player_order and direction
    old_state = {
        "draw_pile": [f"Green {i}" for i in range(10)] * 8,
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Red 3", "Blue 3", "Green 7", "Yellow 1", "Red 2", "Blue 8", "Green 4"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        # NOTE: no player_order, no direction
    }
    await r.set(f"uno:{game_id}", json.dumps(old_state))

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        # Status should work (migration fills in defaults)
        text_a, err = await pa.call("status")
        assert not err, f"Status failed on old state: {text_a}"
        assert "YOUR TURN" in text_a, f"Expected YOUR TURN, got: {parse_status_line(text_a)}"
        assert "Opponent has: 7 cards" in text_a, f"Expected opponent info in status"

        # Play should work
        text, err = await pa.call("play", {"card": "Red 3"})
        assert not err, f"Play failed on old state: {text}"
        assert text == "You played Red 3.", f"Unexpected: {text}"

        # Verify turn passed correctly
        text_b, err = await pb.call("status")
        assert not err, f"B status failed: {text_b}"
        assert "YOUR TURN" in text_b, f"Expected B's turn after A played"

        log("  PASS: Old game state (no player_order/direction) works via migration.")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 10: Web server responds correctly during 2-player game
# ---------------------------------------------------------------------------
async def test_web_server_2p():
    log("\n--- Test: Web server during 2-player game ---")
    game_id = f"reg_web_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        # Give web servers a moment to start
        await asyncio.sleep(0.5)

        async with aiohttp.ClientSession() as http:
            for pid, port in [("A", 19000), ("B", 19001)]:
                url = f"http://localhost:{port}/"
                async with http.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    assert resp.status == 200, f"Player {pid} web server returned {resp.status}"
                    body = await resp.text()
                    assert f"Player {pid}" in body, f"Page missing player name"
                    assert "text/html" in resp.content_type, f"Wrong content type"
                    # Verify it has game data (hand section)
                    assert "Your Hand" in body, f"Missing hand section"
                    log(f"  Player {pid} web server at :{port} OK")

        # Now play a card via MCP and verify web server still works
        status_a, _ = await pa.call("status")
        sl = parse_status_line(status_a)
        if sl == "YOUR TURN":
            await pa.call("draw")
        else:
            await pb.call("draw")

        async with aiohttp.ClientSession() as http:
            async with http.get("http://localhost:19000/", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                assert resp.status == 200, "Web server broke after MCP action"

        log("  PASS: Web servers work correctly alongside MCP.")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 11: Wait tool still works in 2-player
# ---------------------------------------------------------------------------
async def test_wait_2p():
    log("\n--- Test: Wait tool in 2-player ---")
    game_id = f"reg_wait_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    state = {
        "draw_pile": [f"Green {i}" for i in range(10)] * 8,
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Red 3", "Blue 3", "Green 7", "Yellow 1", "Red 2", "Blue 8", "Green 4"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        # Wait when it's already your turn — should return immediately
        text, err = await pa.call("wait", {"timeout": 5})
        assert not err, f"Wait error: {text}"
        assert "Game started" in text, f"Expected 'Game started', got: {text}"

        # Player A plays, then B waits (should return immediately since state changed)
        text, err = await pa.call("play", {"card": "Red 3"})
        assert not err, f"Play error: {text}"

        text, err = await pb.call("wait", {"timeout": 5})
        assert not err, f"Wait error: {text}"
        assert "Player A played Red 3" in text, f"Expected last action, got: {text}"

        log("  PASS: Wait tool works correctly in 2-player.")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 12: Win detection in 2-player
# ---------------------------------------------------------------------------
async def test_win_2p():
    log("\n--- Test: Win detection in 2-player ---")
    game_id = f"reg_win_{uuid.uuid4().hex[:8]}"
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    state = {
        "draw_pile": [f"Green {i}" for i in range(10)] * 8,
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Red 3"],  # Only 1 card — playing it wins
            "B": ["Red 1", "Blue 2", "Green 3"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    pa = MCPPlayer("A")
    pb = MCPPlayer("B")
    try:
        await pa.start(game_id, "A")
        await pb.start(game_id, "B")

        text, err = await pa.call("play", {"card": "Red 3"})
        assert not err, f"Error: {text}"
        assert "You win" in text, f"Expected win message, got: {text}"

        # Check statuses
        text_a, _ = await pa.call("status")
        assert "YOU WON!" in text_a, f"A should see YOU WON!, got: {parse_status_line(text_a)}"

        text_b, _ = await pb.call("status")
        assert "OPPONENT WON!" in text_b, f"B should see OPPONENT WON!, got: {parse_status_line(text_b)}"

        # Game should be over — further plays should error
        text, err = await pb.call("draw")
        assert err, "Should error drawing after game over"
        assert "already over" in text.lower(), f"Expected 'already over', got: {text}"

        log("  PASS: Win detection and game-over state correct.")
    finally:
        await pb.stop()
        await pa.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


# ---------------------------------------------------------------------------
# Test 13: Run multiple full 2-player games (exercise randomness)
# ---------------------------------------------------------------------------
async def test_full_games_2p(num_games: int = 3):
    log(f"\n--- Test: {num_games} full 2-player games ---")

    for i in range(num_games):
        game_id = f"reg_full_{i}_{uuid.uuid4().hex[:8]}"
        r = aioredis.Redis(decode_responses=True)
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

        pa = MCPPlayer("A")
        pb = MCPPlayer("B")
        try:
            await pa.start(game_id, "A")
            await pb.start(game_id, "B")

            players = {"A": pa, "B": pb}
            turn_count = 0

            while turn_count < 300:
                turn_count += 1

                # Determine whose turn from A's perspective
                text_a, _ = await pa.call("status")
                sl = parse_status_line(text_a)

                if sl in ("YOU WON!", "OPPONENT WON!"):
                    break

                cur_id = "A" if sl == "YOUR TURN" else "B"
                cur = players[cur_id]

                st, _ = await cur.call("status")
                hand = parse_hand_from_status(st)
                top = ""
                color = ""
                for line in st.splitlines():
                    if line.startswith("Top card: "):
                        top = line[len("Top card: "):]
                    if line.startswith("Current color: "):
                        color = line[len("Current color: "):]

                # Try to play
                played = False
                for card in hand:
                    wild = card in ("Wild", "Wild Draw Four")
                    if wild:
                        can_play = True
                    else:
                        parts = card.split(" ", 1)
                        tparts = top.split(" ", 1)
                        can_play = (len(parts) >= 2 and len(tparts) >= 2 and
                                    (parts[0] == color or parts[1] == tparts[1]))
                    if can_play:
                        args = {"card": card}
                        if wild:
                            args["chosen_color"] = random.choice(COLORS)
                        text, err = await cur.call("play", args)
                        assert not err, f"Error playing {card}: {text}"
                        played = True
                        break

                if not played:
                    text, err = await cur.call("draw")
                    assert not err, f"Error drawing: {text}"

            # Validate final state
            raw = await r.get(f"uno:{game_id}")
            s = json.loads(raw)
            total = len(s["hands"]["A"]) + len(s["hands"]["B"]) + len(s["draw_pile"]) + len(s["discard_pile"])
            assert total == 108, f"Game {i}: card conservation violated: {total}"

            if s["winner"]:
                assert len(s["hands"][s["winner"]]) == 0, f"Game {i}: winner should have 0 cards"

            log(f"  Game {i+1}: {turn_count} turns, winner={'Player ' + s['winner'] if s['winner'] else 'none (limit)'}")
        finally:
            await pb.stop()
            await pa.stop()
            await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
            await r.close()

    log(f"  PASS: All {num_games} full 2-player games valid.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    await test_skip_2p()
    await test_reverse_2p()
    await test_draw_two_2p()
    await test_wild_draw_four_2p()
    await test_wild_2p()
    await test_number_2p()
    await test_draw_2p()
    await test_status_format_2p()
    await test_old_state_migration()
    await test_web_server_2p()
    await test_wait_2p()
    await test_win_2p()
    await test_full_games_2p(3)

    log("\n" + "=" * 60)
    log("=== ALL REGRESSION TESTS PASSED ===")
    log("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
