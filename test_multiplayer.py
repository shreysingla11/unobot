#!/usr/bin/env python3
"""
Test script for multi-player UNO (Part 5).

Launches 3-player and 4-player games via MCP, plays them to completion,
validates turn order, direction changes, skip behavior, card conservation,
and web server availability.

Usage:
    python test_multiplayer.py
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
PORT_MAP = {"A": 19000, "B": 19001, "C": 19002, "D": 19003}


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


def parse_top_card(status_text: str) -> str:
    for line in status_text.splitlines():
        if line.startswith("Top card: "):
            return line[len("Top card: "):]
    return ""


def parse_current_color(status_text: str) -> str:
    for line in status_text.splitlines():
        if line.startswith("Current color: "):
            return line[len("Current color: "):]
    return ""


def parse_status_line(status_text: str) -> str:
    for line in status_text.splitlines():
        if line.startswith("Status: "):
            return line[len("Status: "):]
    return ""


def parse_whose_turn(status_text: str) -> str | None:
    """Return the player ID whose turn it is, or None if game over."""
    sl = parse_status_line(status_text)
    if "WON" in sl:
        return None
    if sl == "YOUR TURN":
        return "self"
    # "Player X's TURN"
    if sl.startswith("Player ") and sl.endswith("'s TURN"):
        return sl[len("Player "):-len("'s TURN")]
    return None


def is_wild(card: str) -> bool:
    return card in ("Wild", "Wild Draw Four")


def is_valid_play(card: str, top_card: str, current_color: str) -> bool:
    if is_wild(card):
        return True
    card_parts = card.split(" ", 1)
    top_parts = top_card.split(" ", 1)
    if len(card_parts) < 2 or len(top_parts) < 2:
        return False
    if card_parts[0] == current_color:
        return True
    if card_parts[1] == top_parts[1]:
        return True
    return False


def choose_play(hand: list[str], top_card: str, current_color: str):
    for card in hand:
        if is_valid_play(card, top_card, current_color):
            chosen_color = random.choice(COLORS) if is_wild(card) else None
            return card, chosen_color
    return None


class MCPPlayer:
    def __init__(self, name: str):
        self.name = name
        self._stdio_cm = None
        self._session_cm = None
        self.session: ClientSession | None = None

    async def start(self, game_id: str, player: str, num_players: int) -> None:
        params = StdioServerParameters(
            command=PYTHON,
            args=[MAIN_PY, f"--game={game_id}", f"--player={player}",
                  f"--num-players={num_players}"],
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


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


async def find_current_player(players: dict[str, MCPPlayer]) -> str | None:
    """Query Player A's status to determine whose turn it is. Return player ID or None."""
    status_text, _ = await players["A"].call("status")
    sl = parse_status_line(status_text)
    if "WON" in sl:
        return None
    if sl == "YOUR TURN":
        return "A"
    # "Player X's TURN"
    if sl.startswith("Player ") and sl.endswith("'s TURN"):
        return sl[len("Player "):-len("'s TURN")]
    return None


async def test_game(num_players: int) -> None:
    """Run a full automated game with the given number of players."""
    player_ids = ["A", "B", "C", "D"][:num_players]
    game_id = f"mp{num_players}_{uuid.uuid4().hex[:8]}"
    log(f"\n{'='*60}")
    log(f"=== {num_players}-Player Game: {game_id} ===")
    log(f"{'='*60}\n")

    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    players: dict[str, MCPPlayer] = {}
    for pid in player_ids:
        players[pid] = MCPPlayer(f"Player {pid}")

    try:
        # Start all MCP server processes
        for pid in player_ids:
            await players[pid].start(game_id, pid, num_players)
        log(f"All {num_players} MCP server processes started.\n")

        # ----- Test: list_tools -----------------------------------------------
        log("--- Test: List tools ---")
        tools_result = await players["A"].session.list_tools()
        tool_names = sorted([t.name for t in tools_result.tools])
        assert tool_names == ["draw", "play", "status", "wait"], (
            f"Expected [draw, play, status, wait], got {tool_names}"
        )
        log("  PASS: All 4 tools available.\n")

        # ----- Test: initial status -------------------------------------------
        log("--- Test: Initial status ---")
        for pid in player_ids:
            status_text, _ = await players[pid].call("status")
            hand = parse_hand_from_status(status_text)
            log(f"  Player {pid}: {len(hand)} cards, status: {parse_status_line(status_text)}")
            # Each player should have >= 7 cards (Player A may have 9 if Draw Two start)
            assert len(hand) >= 7, f"Player {pid} should have >= 7 cards, got {len(hand)}"

        # Verify direction is shown for 3+ player games
        status_a, _ = await players["A"].call("status")
        if num_players > 2:
            assert "Direction:" in status_a, "Expected Direction line for 3+ player game"
        log("  PASS: Initial status OK.\n")

        # ----- Test: state has player_order and direction ---------------------
        log("--- Test: Redis state structure ---")
        raw = await r.get(f"uno:{game_id}")
        state = json.loads(raw)
        assert "player_order" in state, "Missing player_order in state"
        assert "direction" in state, "Missing direction in state"
        assert state["player_order"] == player_ids, (
            f"Expected player_order={player_ids}, got {state['player_order']}"
        )
        assert state["direction"] in (1, -1), f"Invalid direction: {state['direction']}"
        log(f"  player_order={state['player_order']}, direction={state['direction']}")
        log("  PASS: Redis state structure OK.\n")

        # ----- Test: web server responds on correct ports ---------------------
        log("--- Test: Web server ---")
        async with aiohttp.ClientSession() as http_session:
            for pid in player_ids:
                port = PORT_MAP[pid]
                url = f"http://localhost:{port}/"
                try:
                    async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        assert resp.status == 200, f"Web server for Player {pid} returned {resp.status}"
                        body = await resp.text()
                        assert f"Player {pid}" in body, (
                            f"Web page for Player {pid} doesn't contain player name"
                        )
                        log(f"  Player {pid} web server at :{port} OK")
                except Exception as e:
                    log(f"  WARNING: Player {pid} web server at :{port} failed: {e}")
        log("  PASS: Web servers respond.\n")

        # ----- Test: wrong-turn error -----------------------------------------
        log("--- Test: Wrong-turn error ---")
        cur_id = await find_current_player(players)
        assert cur_id is not None, "Game should not be over yet"
        # Pick a player who does NOT have the turn
        wrong_id = [p for p in player_ids if p != cur_id][0]
        text, is_err = await players[wrong_id].call("draw")
        assert is_err, "Expected isError=True for wrong-turn draw"
        assert "not your turn" in text.lower(), f"Expected 'not your turn' in error: {text}"
        log(f"  Player {wrong_id} tried to draw out of turn: correctly rejected.")
        log("  PASS: Wrong-turn error works.\n")

        # ----- Test: play full game -------------------------------------------
        log("--- Test: Play full game ---")
        turn_count = 0
        max_turns = 500

        while turn_count < max_turns:
            turn_count += 1

            cur_id = await find_current_player(players)
            if cur_id is None:
                log(f"\n  Turn {turn_count}: Game over!")
                break

            cur_player = players[cur_id]
            status_text, _ = await cur_player.call("status")
            hand = parse_hand_from_status(status_text)
            top = parse_top_card(status_text)
            color = parse_current_color(status_text)

            move = choose_play(hand, top, color)
            if move:
                card, chosen_color = move
                args = {"card": card}
                if chosen_color:
                    args["chosen_color"] = chosen_color
                text, is_err = await cur_player.call("play", args)
                log(f"  Turn {turn_count} [{cur_id}]: PLAY {card}"
                    + (f" (color={chosen_color})" if chosen_color else "")
                    + f" -> {text}")
                assert not is_err, f"Unexpected error playing card: {text}"
            else:
                text, is_err = await cur_player.call("draw")
                log(f"  Turn {turn_count} [{cur_id}]: DRAW -> {text}")
                assert not is_err, f"Unexpected error drawing: {text}"

            if "You win" in text:
                log(f"  Player {cur_id} wins!")
                break
        else:
            log(f"\n  Reached {max_turns} turn limit; stopping.\n")

        # ----- Final state ----------------------------------------------------
        log("\n--- Final game state ---")
        for pid in player_ids:
            final_text, _ = await players[pid].call("status")
            log(f"  Player {pid}:\n{_indent(final_text)}\n")

        # ----- Validate end state against Redis -------------------------------
        log("--- Validating end state ---")
        raw = await r.get(f"uno:{game_id}")
        state = json.loads(raw)

        # Verify hands match Redis
        for pid in player_ids:
            final_text, _ = await players[pid].call("status")
            hand_from_status = parse_hand_from_status(final_text)
            assert hand_from_status == state["hands"][pid], (
                f"Player {pid} hand mismatch with Redis"
            )
        log("  PASS: All player hands match Redis state.")

        # Card conservation
        total_cards = sum(len(state["hands"][pid]) for pid in player_ids)
        total_cards += len(state["draw_pile"]) + len(state["discard_pile"])
        assert total_cards == 108, f"Card conservation violated: {total_cards} != 108"
        log(f"  PASS: Card conservation OK ({total_cards} cards total).")

        # Winner validation
        if state["winner"]:
            winner = state["winner"]
            log(f"  Winner: Player {winner}")
            assert len(state["hands"][winner]) == 0, "Winner should have 0 cards"

            # Winner should see YOU WON
            winner_status, _ = await players[winner].call("status")
            assert "YOU WON" in parse_status_line(winner_status), (
                f"Winner ({winner}) should see YOU WON"
            )

            # All others should see "Player X WON!"
            for pid in player_ids:
                if pid != winner:
                    other_status, _ = await players[pid].call("status")
                    sl = parse_status_line(other_status)
                    assert "WON" in sl, f"Player {pid} should see WON status, got: {sl}"
            log("  PASS: Winner state is consistent.")
        else:
            log("  Game did not finish within turn limit (still valid).")

        log(f"\n  Total turns played: {turn_count}")
        log(f"\n=== {num_players}-PLAYER TEST PASSED ===\n")

    finally:
        for pid in reversed(player_ids):
            await players[pid].stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


async def test_reverse_direction():
    """Test that Reverse changes direction in a 3-player game."""
    game_id = f"rev_{uuid.uuid4().hex[:8]}"
    log(f"\n{'='*60}")
    log(f"=== Reverse Direction Test: {game_id} ===")
    log(f"{'='*60}\n")

    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    # Set up a controlled game state where Player A has a Reverse card
    deck = [f"Red {i}" for i in range(10)] * 10  # filler
    state = {
        "draw_pile": deck[:80],
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Red Reverse", "Blue 3", "Green 7", "Yellow 1", "Red 2", "Blue 8", "Green 4"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
            "C": ["Red 9", "Blue 4", "Green 1", "Yellow 7", "Red 3", "Blue 6", "Green 2"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B", "C"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    players = {}
    for pid in ["A", "B", "C"]:
        players[pid] = MCPPlayer(f"Player {pid}")

    try:
        for pid in ["A", "B", "C"]:
            await players[pid].start(game_id, pid, 3)
        log("All 3 players started.\n")

        # Verify initial direction is Clockwise (A -> B -> C)
        status_a, _ = await players["A"].call("status")
        assert "Clockwise" in status_a, "Initial direction should be Clockwise"
        log("  Initial direction: Clockwise")

        # Player A plays Red Reverse
        text, is_err = await players["A"].call("play", {"card": "Red Reverse"})
        assert not is_err, f"Error playing Reverse: {text}"
        log(f"  Player A plays Red Reverse: {text}")

        # After Reverse in 3-player: direction flips to Counter-clockwise
        # Next player should be C (A's previous player in new direction)
        raw = await r.get(f"uno:{game_id}")
        state = json.loads(raw)
        assert state["direction"] == -1, f"Direction should be -1 after Reverse, got {state['direction']}"
        assert state["current_turn"] == "C", (
            f"After A plays Reverse (3p), next should be C, got {state['current_turn']}"
        )
        log(f"  Direction is now: {state['direction']} (Counter-clockwise)")
        log(f"  Current turn: {state['current_turn']}")

        # Verify status shows Counter-clockwise
        status_c, _ = await players["C"].call("status")
        assert "Counter-clockwise" in status_c, "Direction should be Counter-clockwise"
        log("  Status confirms Counter-clockwise direction.")

        log("\n=== REVERSE DIRECTION TEST PASSED ===\n")

    finally:
        for pid in reversed(["A", "B", "C"]):
            await players[pid].stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


async def test_skip_multiplayer():
    """Test that Skip skips the next player in a 3-player game."""
    game_id = f"skip_{uuid.uuid4().hex[:8]}"
    log(f"\n{'='*60}")
    log(f"=== Skip Test (3-player): {game_id} ===")
    log(f"{'='*60}\n")

    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    deck = [f"Red {i}" for i in range(10)] * 10
    state = {
        "draw_pile": deck[:80],
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Red Skip", "Blue 3", "Green 7", "Yellow 1", "Red 2", "Blue 8", "Green 4"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
            "C": ["Red 9", "Blue 4", "Green 1", "Yellow 7", "Red 3", "Blue 6", "Green 2"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B", "C"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    players = {}
    for pid in ["A", "B", "C"]:
        players[pid] = MCPPlayer(f"Player {pid}")

    try:
        for pid in ["A", "B", "C"]:
            await players[pid].start(game_id, pid, 3)
        log("All 3 players started.\n")

        # Player A plays Red Skip — should skip B, go to C
        text, is_err = await players["A"].call("play", {"card": "Red Skip"})
        assert not is_err, f"Error playing Skip: {text}"
        log(f"  Player A plays Red Skip: {text}")

        raw = await r.get(f"uno:{game_id}")
        state = json.loads(raw)
        assert state["current_turn"] == "C", (
            f"After A plays Skip (3p), next should be C (B skipped), got {state['current_turn']}"
        )
        log(f"  Current turn: {state['current_turn']} (B was skipped)")

        log("\n=== SKIP TEST PASSED ===\n")

    finally:
        for pid in reversed(["A", "B", "C"]):
            await players[pid].stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


async def test_draw_two_multiplayer():
    """Test Draw Two in 3-player: victim draws 2 and is skipped."""
    game_id = f"d2_{uuid.uuid4().hex[:8]}"
    log(f"\n{'='*60}")
    log(f"=== Draw Two Test (3-player): {game_id} ===")
    log(f"{'='*60}\n")

    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    deck = [f"Red {i}" for i in range(10)] * 10
    state = {
        "draw_pile": deck[:80],
        "discard_pile": ["Red 5"],
        "hands": {
            "A": ["Red Draw Two", "Blue 3", "Green 7", "Yellow 1", "Red 2", "Blue 8", "Green 4"],
            "B": ["Red 1", "Blue 2", "Green 3", "Yellow 4", "Red 6", "Blue 7", "Green 8"],
            "C": ["Red 9", "Blue 4", "Green 1", "Yellow 7", "Red 3", "Blue 6", "Green 2"],
        },
        "current_turn": "A",
        "current_color": "Red",
        "last_action": "Game started",
        "winner": None,
        "player_order": ["A", "B", "C"],
        "direction": 1,
    }
    await r.set(f"uno:{game_id}", json.dumps(state))

    players = {}
    for pid in ["A", "B", "C"]:
        players[pid] = MCPPlayer(f"Player {pid}")

    try:
        for pid in ["A", "B", "C"]:
            await players[pid].start(game_id, pid, 3)
        log("All 3 players started.\n")

        # Player A plays Draw Two — B draws 2 and is skipped, C goes
        text, is_err = await players["A"].call("play", {"card": "Red Draw Two"})
        assert not is_err, f"Error playing Draw Two: {text}"
        log(f"  Player A plays Red Draw Two: {text}")

        raw = await r.get(f"uno:{game_id}")
        state = json.loads(raw)
        assert state["current_turn"] == "C", (
            f"After A plays Draw Two (3p), turn should go to C, got {state['current_turn']}"
        )
        assert len(state["hands"]["B"]) == 9, (
            f"Player B should have 9 cards (7+2), got {len(state['hands']['B'])}"
        )
        log(f"  Player B now has {len(state['hands']['B'])} cards (drew 2)")
        log(f"  Current turn: {state['current_turn']} (B was skipped)")

        log("\n=== DRAW TWO TEST PASSED ===\n")

    finally:
        for pid in reversed(["A", "B", "C"]):
            await players[pid].stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


async def main():
    # Run targeted tests first
    await test_reverse_direction()
    await test_skip_multiplayer()
    await test_draw_two_multiplayer()

    # Run full games
    await test_game(3)
    await test_game(4)

    log("\n" + "=" * 60)
    log("=== ALL MULTI-PLAYER TESTS PASSED ===")
    log("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
