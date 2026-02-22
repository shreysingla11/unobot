#!/usr/bin/env python3
"""
Test script for the UNO MCP server (Part 2).

Launches two MCP server sub-processes (Player A and Player B) and drives a
full game through MCP tool calls, exercising status, play, draw, and error
paths.  Prints every interaction and validates the final game state.

Usage:
    python test.py
"""

import asyncio
import json
import os
import random
import sys
import uuid

import redis.asyncio as aioredis
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# Resolve paths
PYTHON = sys.executable
MAIN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")

COLORS = ["Red", "Yellow", "Green", "Blue"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def extract_text(result) -> str:
    """Pull the text string out of a CallToolResult."""
    return result.content[0].text


def parse_hand_from_status(status_text: str) -> list[str]:
    """Parse the player's hand from the status output."""
    cards = []
    in_hand = False
    for line in status_text.splitlines():
        if line.strip() == "=== Your Hand ===":
            in_hand = True
            continue
        if line.strip() == "" and in_hand:
            break
        if in_hand:
            # Lines look like: " 1. Red 3"
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


def is_wild(card: str) -> bool:
    return card in ("Wild", "Wild Draw Four")


def is_valid_play(card: str, top_card: str, current_color: str) -> bool:
    if is_wild(card):
        return True
    card_parts = card.split(" ", 1)
    top_parts = top_card.split(" ", 1)
    if len(card_parts) < 2 or len(top_parts) < 2:
        return False
    card_color = card_parts[0]
    card_value = card_parts[1]
    top_value = top_parts[1]
    if card_color == current_color:
        return True
    if card_value == top_value:
        return True
    return False


def choose_play(hand: list[str], top_card: str, current_color: str):
    """Pick a card to play.  Returns (card, chosen_color) or None."""
    for card in hand:
        if is_valid_play(card, top_card, current_color):
            chosen_color = random.choice(COLORS) if is_wild(card) else None
            return card, chosen_color
    return None


# ---------------------------------------------------------------------------
# MCP client wrapper
# ---------------------------------------------------------------------------

class MCPPlayer:
    """Wraps an MCP client session connected to one player's server process."""

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

    async def call(self, tool: str, arguments: dict | None = None) -> str:
        result = await self.session.call_tool(tool, arguments or {})
        text = extract_text(result)
        is_err = getattr(result, "isError", False)
        return text, is_err


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

async def test_uno():
    game_id = f"test_{uuid.uuid4().hex[:8]}"
    log(f"=== Starting UNO test game: {game_id} ===\n")

    # Clean up any leftover state
    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    player_a = MCPPlayer("Player A")
    player_b = MCPPlayer("Player B")

    try:
        # Start both MCP server processes
        await player_a.start(game_id, "A")
        await player_b.start(game_id, "B")
        log("Both MCP server processes started.\n")

        # ----- Test 1: list_tools --------------------------------------------
        log("--- Test 1: List tools ---")
        tools_result = await player_a.session.list_tools()
        tool_names = sorted([t.name for t in tools_result.tools])
        log(f"  Available tools: {tool_names}")
        assert tool_names == ["draw", "play", "status", "wait"], (
            f"Expected [draw, play, status, wait], got {tool_names}"
        )
        log("  PASS: All 4 tools available.\n")

        # ----- Test 2: initial status for both players -----------------------
        log("--- Test 2: Initial status ---")
        status_a, _ = await player_a.call("status")
        status_b, _ = await player_b.call("status")
        log(f"  Player A status:\n{_indent(status_a)}\n")
        log(f"  Player B status:\n{_indent(status_b)}\n")

        hand_a = parse_hand_from_status(status_a)
        hand_b = parse_hand_from_status(status_b)
        assert len(hand_a) >= 7, f"Player A should have >= 7 cards, got {len(hand_a)}"
        assert len(hand_b) == 7, f"Player B should have 7 cards, got {len(hand_b)}"

        status_line_a = parse_status_line(status_a)
        status_line_b = parse_status_line(status_b)
        # Exactly one should have YOUR TURN
        turns = [status_line_a, status_line_b]
        assert turns.count("YOUR TURN") == 1, f"Expected exactly 1 YOUR TURN, got {turns}"
        assert turns.count("OPPONENT'S TURN") == 1, f"Expected exactly 1 OPPONENT'S TURN, got {turns}"
        log("  PASS: Both players see consistent initial state.\n")

        # ----- Test 3: Error – wrong turn ------------------------------------
        log("--- Test 3: Wrong-turn error ---")
        # Figure out who does NOT have the turn
        if status_line_a == "YOUR TURN":
            wrong_player = player_b
            wrong_name = "Player B"
        else:
            wrong_player = player_a
            wrong_name = "Player A"
        text, is_err = await wrong_player.call("draw")
        log(f"  {wrong_name} tried to draw out of turn:")
        log(f"    Response: {text}")
        log(f"    isError: {is_err}")
        assert is_err, "Expected isError=True for wrong-turn draw"
        assert "not your turn" in text.lower(), f"Expected 'not your turn' in error: {text}"
        log("  PASS: Wrong-turn error works.\n")

        # ----- Test 4: Error – invalid card ----------------------------------
        log("--- Test 4: Invalid card error ---")
        current = player_a if status_line_a == "YOUR TURN" else player_b
        cur_label = "Player A" if current is player_a else "Player B"
        text, is_err = await current.call("play", {"card": "Fake Card 99"})
        log(f"  {cur_label} tried to play 'Fake Card 99':")
        log(f"    Response: {text}")
        log(f"    isError: {is_err}")
        assert is_err, "Expected isError=True for card-not-in-hand"
        log("  PASS: Invalid card error works.\n")

        # ----- Test 5: Play a full game --------------------------------------
        log("--- Test 5: Play full game ---")
        players = {"A": player_a, "B": player_b}
        turn_count = 0
        max_turns = 300  # safety limit

        while turn_count < max_turns:
            turn_count += 1

            # Get status from both to find whose turn it is
            status_a_text, _ = await player_a.call("status")
            sl = parse_status_line(status_a_text)

            if sl in ("YOU WON!", "OPPONENT WON!"):
                log(f"\n  Turn {turn_count}: Game over!")
                log(f"  Player A sees: {sl}")
                break

            if sl == "YOUR TURN":
                cur_id = "A"
            else:
                cur_id = "B"

            cur_player = players[cur_id]

            # Get current player's status
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
                    + f" → {text}")
                assert not is_err, f"Unexpected error playing card: {text}"
            else:
                text, is_err = await cur_player.call("draw")
                log(f"  Turn {turn_count} [{cur_id}]: DRAW → {text}")
                assert not is_err, f"Unexpected error drawing: {text}"
        else:
            # If we hit max_turns, game is still valid – just long
            log(f"\n  Reached {max_turns} turn limit; stopping.\n")

        # ----- Final state ---------------------------------------------------
        log("\n--- Final game state ---")
        final_a, _ = await player_a.call("status")
        final_b, _ = await player_b.call("status")
        log(f"  Player A:\n{_indent(final_a)}\n")
        log(f"  Player B:\n{_indent(final_b)}\n")

        # ----- Validate end state against Redis ------------------------------
        log("--- Validating end state ---")
        raw = await r.get(f"uno:{game_id}")
        state = json.loads(raw)

        final_hand_a = parse_hand_from_status(final_a)
        final_hand_b = parse_hand_from_status(final_b)
        assert final_hand_a == state["hands"]["A"], "Player A hand mismatch with Redis"
        assert final_hand_b == state["hands"]["B"], "Player B hand mismatch with Redis"
        log("  PASS: Player hands match Redis state.")

        total_cards = (
            len(state["hands"]["A"])
            + len(state["hands"]["B"])
            + len(state["draw_pile"])
            + len(state["discard_pile"])
        )
        assert total_cards == 108, f"Card conservation violated: {total_cards} != 108"
        log(f"  PASS: Card conservation OK ({total_cards} cards total).")

        sl_a = parse_status_line(final_a)
        sl_b = parse_status_line(final_b)
        if state["winner"]:
            winner = state["winner"]
            log(f"  Winner: Player {winner}")
            if winner == "A":
                assert sl_a == "YOU WON!", f"Player A should see YOU WON!, got {sl_a}"
                assert sl_b == "OPPONENT WON!", f"Player B should see OPPONENT WON!, got {sl_b}"
                assert len(state["hands"]["A"]) == 0, "Winner should have 0 cards"
            else:
                assert sl_b == "YOU WON!", f"Player B should see YOU WON!, got {sl_b}"
                assert sl_a == "OPPONENT WON!", f"Player A should see OPPONENT WON!, got {sl_a}"
                assert len(state["hands"]["B"]) == 0, "Winner should have 0 cards"
            log("  PASS: Winner state is consistent.")
        else:
            log("  Game did not finish within turn limit (still valid).")

        log(f"\n  Total turns played: {turn_count}")
        log("\n=== ALL TESTS PASSED ===")

    finally:
        await player_b.stop()
        await player_a.stop()
        # Clean up Redis
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.close()


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


async def main():
    await test_uno()


if __name__ == "__main__":
    asyncio.run(main())
