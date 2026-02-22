#!/usr/bin/env python3
"""Automated UNO player for testing against an LLM opponent (Part 4).

Connects to a game via MCP and plays automatically using a simple strategy
(first valid card). Uses the wait tool for turn coordination.

Usage:
    python auto_player.py --game=<game_id> --player=A [--num-players=3]
"""

import argparse
import asyncio
import os
import random
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PYTHON = sys.executable
MAIN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
COLORS = ["Red", "Yellow", "Green", "Blue"]


def log(msg: str) -> None:
    print(msg, flush=True)


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


async def auto_play(game_id: str, player: str, num_players: int = 2):
    params = StdioServerParameters(
        command=PYTHON,
        args=[MAIN_PY, f"--game={game_id}", f"--player={player}",
              f"--num-players={num_players}"],
    )

    stdio_cm = stdio_client(params)
    read_stream, write_stream = await stdio_cm.__aenter__()
    session_cm = ClientSession(read_stream, write_stream)
    session = await session_cm.__aenter__()
    await session.initialize()

    try:
        log(f"[Auto {player}] Connected to game {game_id}")
        turn = 0

        while True:
            turn += 1

            # Wait for our turn (generous timeout for slow LLM opponent)
            result = await session.call_tool("wait", {"timeout": 300})
            wait_text = result.content[0].text

            # Check game state
            result = await session.call_tool("status", {})
            status_text = result.content[0].text
            sl = parse_status_line(status_text)

            if "WON" in sl:
                log(f"[Auto {player}] Game over: {sl}")
                break

            hand = parse_hand_from_status(status_text)
            top = parse_top_card(status_text)
            color = parse_current_color(status_text)

            move = choose_play(hand, top, color)
            if move:
                card, chosen_color = move
                args = {"card": card}
                if chosen_color:
                    args["chosen_color"] = chosen_color
                result = await session.call_tool("play", args)
                text = result.content[0].text
                log(
                    f"[Auto {player}] Turn {turn}: PLAY {card}"
                    + (f" ({chosen_color})" if chosen_color else "")
                    + f" -> {text}"
                )
            else:
                result = await session.call_tool("draw", {})
                text = result.content[0].text
                log(f"[Auto {player}] Turn {turn}: DRAW -> {text}")

            if "You win" in text:
                log(f"[Auto {player}] I won!")
                break
    finally:
        await session_cm.__aexit__(None, None, None)
        await stdio_cm.__aexit__(None, None, None)


async def main():
    parser = argparse.ArgumentParser(description="Automated UNO player")
    parser.add_argument("--game", required=True, help="Game ID")
    parser.add_argument(
        "--player", required=True, choices=["A", "B", "C", "D"],
        help="Player (A-D)",
    )
    parser.add_argument(
        "--num-players", type=int, default=2, choices=[2, 3, 4],
        help="Number of players (default: 2)",
    )
    args = parser.parse_args()

    await auto_play(args.game, args.player, args.num_players)


if __name__ == "__main__":
    asyncio.run(main())
