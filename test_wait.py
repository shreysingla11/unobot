#!/usr/bin/env python3
"""
Test script for the Wait tool (Part 3).

Launches two MCP server sub-processes and drives a full game using the Wait
tool for turn coordination instead of polling Status.

Usage:
    python test_wait.py
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
    for card in hand:
        if is_valid_play(card, top_card, current_color):
            chosen_color = random.choice(COLORS) if is_wild(card) else None
            return card, chosen_color
    return None


# ---------------------------------------------------------------------------
# MCP client wrapper
# ---------------------------------------------------------------------------

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


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_wait():
    game_id = f"test_wait_{uuid.uuid4().hex[:8]}"
    log(f"=== Starting UNO Wait-tool test: {game_id} ===\n")

    r = aioredis.Redis(decode_responses=True)
    await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")

    player_a = MCPPlayer("Player A")
    player_b = MCPPlayer("Player B")

    try:
        await player_a.start(game_id, "A")
        await player_b.start(game_id, "B")
        log("Both MCP server processes started.\n")

        # ----- Test 1: list_tools includes wait --------------------------------
        log("--- Test 1: Wait tool appears in list_tools ---")
        tools_result = await player_a.session.list_tools()
        tool_names = sorted([t.name for t in tools_result.tools])
        log(f"  Available tools: {tool_names}")
        assert tool_names == ["draw", "play", "status", "wait"], (
            f"Expected [draw, play, status, wait], got {tool_names}"
        )
        log("  PASS: All 4 tools available.\n")

        # ----- Test 2: Wait when already your turn -----------------------------
        log("--- Test 2: Wait when already your turn ---")
        # Determine who has the first turn
        status_a, _ = await player_a.call("status")
        sl_a = parse_status_line(status_a)
        if sl_a == "YOUR TURN":
            first_player, first_id = player_a, "A"
            second_player, second_id = player_b, "B"
        else:
            first_player, first_id = player_b, "B"
            second_player, second_id = player_a, "A"
        log(f"  First turn: Player {first_id}")

        # The player whose turn it is should get an immediate response from wait
        wait_text, wait_err = await first_player.call("wait", {"timeout": 5})
        log(f"  Player {first_id} called wait (already their turn):")
        log(f"    Response: {wait_text}")
        log(f"    isError: {wait_err}")
        assert not wait_err, f"Wait should not error when it's your turn: {wait_text}"
        assert "Game started" in wait_text, (
            f"Expected 'Game started' in wait response, got: {wait_text}"
        )
        log("  PASS: Wait returns immediately when it's already your turn.\n")

        # ----- Test 3: Play full game using Wait for coordination --------------
        log("--- Test 3: Full game with Wait-based turn coordination ---")
        players = {"A": player_a, "B": player_b}
        turn_count = 0
        max_turns = 300

        # Determine initial active player
        active_id = first_id
        other_id = second_id

        while turn_count < max_turns:
            turn_count += 1

            active = players[active_id]
            other = players[other_id]

            # Get active player's status to decide on a move
            status_text, _ = await active.call("status")
            sl = parse_status_line(status_text)

            if sl in ("YOU WON!", "OPPONENT WON!"):
                log(f"\n  Turn {turn_count}: Game over!")
                break

            assert sl == "YOUR TURN", (
                f"Expected YOUR TURN for Player {active_id}, got: {sl}"
            )

            hand = parse_hand_from_status(status_text)
            top = parse_top_card(status_text)
            color = parse_current_color(status_text)

            move = choose_play(hand, top, color)
            if move:
                card, chosen_color = move
                args = {"card": card}
                if chosen_color:
                    args["chosen_color"] = chosen_color
                text, is_err = await active.call("play", args)
                log(f"  Turn {turn_count} [{active_id}]: PLAY {card}"
                    + (f" (color={chosen_color})" if chosen_color else "")
                    + f" -> {text}")
                assert not is_err, f"Unexpected error playing: {text}"
            else:
                text, is_err = await active.call("draw")
                log(f"  Turn {turn_count} [{active_id}]: DRAW -> {text}")
                assert not is_err, f"Unexpected error drawing: {text}"

            # Check if the game ended with this move
            if "You win!" in text:
                log(f"\n  Player {active_id} won!")
                break

            # Determine who has the next turn by reading state
            state = json.loads(await r.get(f"uno:{game_id}"))
            next_turn = state["current_turn"]

            # Only call wait on the other player if the turn actually switched
            # (Skip/Reverse/Draw Two/Wild Draw Four keep the turn)
            if next_turn != active_id:
                wait_text, wait_err = await players[next_turn].call(
                    "wait", {"timeout": 5}
                )
                log(f"         [{next_turn}]: WAIT -> {wait_text}")
                assert not wait_err, f"Wait returned error: {wait_text}"

            active_id = next_turn
            other_id = "B" if next_turn == "A" else "A"
        else:
            log(f"\n  Reached {max_turns} turn limit; stopping.\n")

        # ----- Test 4: Wait after game over ------------------------------------
        log("\n--- Test 4: Wait after game over ---")
        raw = await r.get(f"uno:{game_id}")
        state = json.loads(raw)
        if state["winner"]:
            # Both players should get immediate response from wait
            for pid, p in players.items():
                wt, we = await p.call("wait", {"timeout": 5})
                log(f"  Player {pid} wait after game over: {wt}")
                assert not we, f"Wait should not error after game over: {wt}"
            log("  PASS: Wait returns immediately after game over.\n")
        else:
            log("  Game did not finish; skipping post-game wait test.\n")

        # ----- Final validation ------------------------------------------------
        log("--- Final game state ---")
        final_a, _ = await player_a.call("status")
        final_b, _ = await player_b.call("status")
        log(f"  Player A:\n{_indent(final_a)}\n")
        log(f"  Player B:\n{_indent(final_b)}\n")

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
                assert len(state["hands"]["A"]) == 0
            else:
                assert sl_b == "YOU WON!", f"Player B should see YOU WON!, got {sl_b}"
                assert sl_a == "OPPONENT WON!", f"Player A should see OPPONENT WON!, got {sl_a}"
                assert len(state["hands"]["B"]) == 0
            log("  PASS: Winner state is consistent.")
        else:
            log("  Game did not finish within turn limit (still valid).")

        log(f"\n  Total turns played: {turn_count}")
        log("\n=== ALL TESTS PASSED ===")

    finally:
        await player_b.stop()
        await player_a.stop()
        await r.delete(f"uno:{game_id}", f"uno:{game_id}:lock")
        await r.aclose()


async def main():
    await test_wait()


if __name__ == "__main__":
    asyncio.run(main())
