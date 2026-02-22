import argparse
import asyncio
import json
import random

import redis.asyncio as aioredis
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COLORS = ["Red", "Yellow", "Green", "Blue"]

LOCK_TIMEOUT = 5  # seconds


def build_deck() -> list[str]:
    """Build a standard 108-card UNO deck."""
    deck: list[str] = []
    for color in COLORS:
        # One 0 per color
        deck.append(f"{color} 0")
        # Two each of 1-9
        for n in range(1, 10):
            deck.append(f"{color} {n}")
            deck.append(f"{color} {n}")
        # Two each of Skip, Reverse, Draw Two
        for action in ("Skip", "Reverse", "Draw Two"):
            deck.append(f"{color} {action}")
            deck.append(f"{color} {action}")
    # 4 Wild, 4 Wild Draw Four
    for _ in range(4):
        deck.append("Wild")
        deck.append("Wild Draw Four")
    return deck


def parse_card(card_str: str) -> tuple[str | None, str]:
    """Parse a card string into (color_or_none, type_str).

    Examples:
        "Red 5"           -> ("Red", "5")
        "Green Skip"      -> ("Green", "Skip")
        "Red Draw Two"    -> ("Red", "Draw Two")
        "Wild"            -> (None, "Wild")
        "Wild Draw Four"  -> (None, "Wild Draw Four")
    """
    if card_str == "Wild":
        return None, "Wild"
    if card_str == "Wild Draw Four":
        return None, "Wild Draw Four"
    for color in COLORS:
        if card_str.startswith(color + " "):
            return color, card_str[len(color) + 1 :]
    raise ValueError(f"Cannot parse card: {card_str!r}")


def is_wild(card: str) -> bool:
    return card in ("Wild", "Wild Draw Four")


def is_valid_play(card: str, top_card: str, current_color: str) -> bool:
    """Check whether *card* can legally be played on *top_card* / *current_color*."""
    if is_wild(card):
        return True
    card_color, card_type = parse_card(card)
    _top_color, top_type = parse_card(top_card)
    # Match by current color
    if card_color == current_color:
        return True
    # Match by number / type
    if card_type == top_type:
        return True
    return False


def reshuffle_if_needed(state: dict) -> None:
    """If draw pile is empty, shuffle discard pile (minus top card) back in."""
    if len(state["draw_pile"]) == 0:
        if len(state["discard_pile"]) <= 1:
            return  # nothing to reshuffle
        top = state["discard_pile"][-1]
        reshuffled = state["discard_pile"][:-1]
        random.shuffle(reshuffled)
        state["draw_pile"] = reshuffled
        state["discard_pile"] = [top]


# ---------------------------------------------------------------------------
# UnoGame – manages Redis-backed game state
# ---------------------------------------------------------------------------
class UnoGame:
    def __init__(self, game_id: str, player: str):
        self.game_id = game_id
        self.player = player  # "A" or "B"
        self.opponent = "B" if player == "A" else "A"
        self.redis: aioredis.Redis | None = None
        self._key = f"uno:{game_id}"
        self._lock_key = f"uno:{game_id}:lock"

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        self.redis = aioredis.Redis(decode_responses=True)
        await self.ensure_game_exists()

    async def close(self) -> None:
        if self.redis:
            await self.redis.aclose()

    # -- helpers -------------------------------------------------------------

    async def _acquire_lock(self) -> None:
        """Simple spin-lock via Redis SETNX with expiry."""
        while True:
            acquired = await self.redis.set(
                self._lock_key, "1", nx=True, ex=LOCK_TIMEOUT
            )
            if acquired:
                return
            await asyncio.sleep(0.05)

    async def _release_lock(self) -> None:
        await self.redis.delete(self._lock_key)

    async def get_state(self) -> dict:
        raw = await self.redis.get(self._key)
        if raw is None:
            raise RuntimeError("Game state not found in Redis")
        return json.loads(raw)

    async def _save_state(self, state: dict) -> None:
        await self.redis.set(self._key, json.dumps(state))

    # -- init ----------------------------------------------------------------

    async def ensure_game_exists(self) -> None:
        await self._acquire_lock()
        try:
            exists = await self.redis.exists(self._key)
            if exists:
                return
            deck = build_deck()
            random.shuffle(deck)
            hands = {"A": deck[:7], "B": deck[7:14]}
            remaining = deck[14:]
            # Flip first non-Wild card as starting discard
            start_idx = 0
            while is_wild(remaining[start_idx]):
                start_idx += 1
            start_card = remaining.pop(start_idx)
            start_color, start_type = parse_card(start_card)
            # Determine first turn effects from start card
            current_turn = "A"
            last_action = "Game started"
            if start_type in ("Skip", "Reverse"):
                # Player A loses first turn
                current_turn = "B"
                last_action = f"Game started – {start_card} skips Player A's turn"
            elif start_type == "Draw Two":
                # Player A draws 2 and loses turn
                hands["A"].append(remaining.pop())
                hands["A"].append(remaining.pop())
                current_turn = "B"
                last_action = f"Game started – {start_card}: Player A draws 2 and is skipped"

            state = {
                "draw_pile": remaining,
                "discard_pile": [start_card],
                "hands": hands,
                "current_turn": current_turn,
                "current_color": start_color,
                "last_action": last_action,
                "winner": None,
            }
            await self._save_state(state)
        finally:
            await self._release_lock()

    # -- tools ---------------------------------------------------------------

    async def status(self) -> str:
        state = await self.get_state()
        hand = state["hands"][self.player]
        top_card = state["discard_pile"][-1]
        current_color = state["current_color"]
        draw_count = len(state["draw_pile"])
        opp_count = len(state["hands"][self.opponent])

        lines: list[str] = []
        lines.append("=== Your Hand ===")
        for i, card in enumerate(hand, 1):
            lines.append(f" {i}. {card}")

        lines.append("")
        lines.append("=== Table ===")
        lines.append(f"Top card: {top_card}")
        lines.append(f"Current color: {current_color}")
        lines.append(f"Draw pile: {draw_count} cards")
        lines.append(f"Opponent has: {opp_count} cards")

        lines.append("")
        winner = state["winner"]
        if winner == self.player:
            lines.append("Status: YOU WON!")
        elif winner == self.opponent:
            lines.append("Status: OPPONENT WON!")
        elif state["current_turn"] == self.player:
            lines.append("Status: YOUR TURN")
        else:
            lines.append("Status: OPPONENT'S TURN")

        return "\n".join(lines)

    async def play(self, card: str, chosen_color: str | None = None) -> str:
        await self._acquire_lock()
        try:
            state = await self.get_state()

            if state["winner"]:
                raise ValueError("Game is already over.")

            if state["current_turn"] != self.player:
                raise ValueError("It is not your turn.")

            hand: list[str] = state["hands"][self.player]
            if card not in hand:
                raise ValueError(f"You don't have {card!r} in your hand.")

            top_card = state["discard_pile"][-1]
            current_color = state["current_color"]

            if not is_valid_play(card, top_card, current_color):
                raise ValueError(
                    f"Cannot play {card!r} on {top_card!r} "
                    f"(current color: {current_color})."
                )

            wild = is_wild(card)
            if wild and not chosen_color:
                raise ValueError(
                    "You must choose a color when playing a Wild card. "
                    "Pass chosen_color as one of: Red, Yellow, Green, Blue."
                )
            if wild and chosen_color not in COLORS:
                raise ValueError(
                    f"Invalid chosen color {chosen_color!r}. "
                    f"Must be one of: {', '.join(COLORS)}."
                )

            # Remove from hand, put on discard
            hand.remove(card)
            state["discard_pile"].append(card)

            # Determine new color
            if wild:
                state["current_color"] = chosen_color
            else:
                card_color, _ = parse_card(card)
                state["current_color"] = card_color

            # Apply effects
            _, card_type = parse_card(card)
            opponent_hand: list[str] = state["hands"][self.opponent]
            msg = f"You played {card}."

            if card_type in ("Skip", "Reverse"):
                state["current_turn"] = self.player
                msg += f" {self.opponent} is skipped."
            elif card_type == "Draw Two":
                reshuffle_if_needed(state)
                for _ in range(2):
                    if state["draw_pile"]:
                        opponent_hand.append(state["draw_pile"].pop())
                state["current_turn"] = self.player
                msg += f" {self.opponent} draws 2 and is skipped."
            elif card == "Wild Draw Four":
                reshuffle_if_needed(state)
                for _ in range(4):
                    if state["draw_pile"]:
                        opponent_hand.append(state["draw_pile"].pop())
                state["current_turn"] = self.player
                msg += (
                    f" Color is now {chosen_color}. "
                    f"{self.opponent} draws 4 and is skipped."
                )
            elif card == "Wild":
                state["current_turn"] = self.opponent
                msg += f" Color is now {chosen_color}."
            else:
                # Normal number card
                state["current_turn"] = self.opponent

            # Check win (after effects applied)
            if len(hand) == 0:
                state["winner"] = self.player
                state["last_action"] = (
                    f"Player {self.player} played {card} and won!"
                )
                await self._save_state(state)
                return f"You played {card}. You win!"

            state["last_action"] = (
                f"Player {self.player} played {card}"
                + (f" (chose {chosen_color})" if wild else "")
            )
            await self._save_state(state)
            return msg
        finally:
            await self._release_lock()

    async def draw(self) -> str:
        await self._acquire_lock()
        try:
            state = await self.get_state()

            if state["winner"]:
                raise ValueError("Game is already over.")

            if state["current_turn"] != self.player:
                raise ValueError("It is not your turn.")

            reshuffle_if_needed(state)

            if not state["draw_pile"]:
                raise ValueError("No cards left to draw.")

            drawn = state["draw_pile"].pop()
            state["hands"][self.player].append(drawn)
            state["current_turn"] = self.opponent
            state["last_action"] = f"Player {self.player} drew a card"
            await self._save_state(state)
            return f"You drew: {drawn}"
        finally:
            await self._release_lock()


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------
server = Server("uno")
game: UnoGame | None = None


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="status",
            description="Show the current game state: your hand, the table, and whose turn it is.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="play",
            description=(
                "Play a card from your hand. Provide the full card name "
                '(e.g. "Red 5", "Wild Draw Four"). '
                "For Wild cards you must also provide chosen_color."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "card": {
                        "type": "string",
                        "description": 'Card to play, e.g. "Red 5", "Green Skip", "Wild"',
                    },
                    "chosen_color": {
                        "type": "string",
                        "description": "Required when playing a Wild card. One of: Red, Yellow, Green, Blue.",
                        "enum": ["Red", "Yellow", "Green", "Blue"],
                    },
                },
                "required": ["card"],
            },
        ),
        types.Tool(
            name="draw",
            description="Draw a card from the draw pile. Ends your turn.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    assert game is not None, "Game not initialized"
    if name == "status":
        result = await game.status()
    elif name == "play":
        card = arguments.get("card", "")
        chosen_color = arguments.get("chosen_color")
        result = await game.play(card, chosen_color)
    elif name == "draw":
        result = await game.draw()
    else:
        raise ValueError(f"Unknown tool: {name}")
    return [types.TextContent(type="text", text=result)]


async def main():
    global game

    parser = argparse.ArgumentParser(description="UNO MCP Server")
    parser.add_argument("--game", required=True, help="Game ID")
    parser.add_argument(
        "--player", required=True, choices=["A", "B"], help="Player (A or B)"
    )
    args = parser.parse_args()

    game = UnoGame(args.game, args.player)
    await game.initialize()

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await game.close()


if __name__ == "__main__":
    asyncio.run(main())
