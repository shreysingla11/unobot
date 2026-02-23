import argparse
import asyncio
import html as html_module
import json
import os
import random
import urllib.parse
import uuid

import anthropic
import redis.asyncio as aioredis
from aiohttp import web
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COLORS = ["Red", "Yellow", "Green", "Blue"]

LOCK_TIMEOUT = 5  # seconds

PORT_MAP = {"A": 19000, "B": 19001, "C": 19002, "D": 19003}


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
    def __init__(self, game_id: str, player: str, num_players: int = 2):
        self.game_id = game_id
        self.player = player  # "A", "B", "C", or "D"
        self.num_players = num_players
        self.players = ["A", "B", "C", "D"][:num_players]
        self.redis: aioredis.Redis | None = None
        self._key = f"uno:{game_id}"
        self._lock_key = f"uno:{game_id}:lock"
        self._pub_channel = f"uno:{game_id}:turns"

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        self.redis = aioredis.Redis(decode_responses=True)
        await self.ensure_game_exists()

    async def close(self) -> None:
        if self.redis:
            await self.redis.aclose()

    # -- helpers -------------------------------------------------------------

    def _next_player(self, state: dict, from_player: str, skip: int = 1) -> str:
        """Return the player `skip` steps away from `from_player` in current direction."""
        order = state["player_order"]
        direction = state["direction"]
        idx = order.index(from_player)
        return order[(idx + direction * skip) % len(order)]

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
        state = json.loads(raw)
        # Migrate old 2-player state that lacks multi-player fields
        if "player_order" not in state:
            state["player_order"] = ["A", "B"]
            state["direction"] = 1
        return state

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
            # Deal 7 cards to each player
            hands = {}
            offset = 0
            for pid in self.players:
                hands[pid] = deck[offset : offset + 7]
                offset += 7
            remaining = deck[offset:]
            # Flip first non-Wild card as starting discard
            start_idx = 0
            while is_wild(remaining[start_idx]):
                start_idx += 1
            start_card = remaining.pop(start_idx)
            start_color, start_type = parse_card(start_card)

            player_order = list(self.players)
            direction = 1

            # Determine first turn effects from start card
            current_turn = "A"
            last_action = "Game started"
            if start_type == "Skip":
                # Skip Player A
                current_turn = "B"
                last_action = f"Game started – {start_card} skips Player A's turn"
            elif start_type == "Reverse":
                if len(player_order) == 2:
                    # 2-player: acts as Skip
                    current_turn = "B"
                    last_action = f"Game started – {start_card} skips Player A's turn"
                else:
                    # 3+ players: reverse direction, turn goes to last player
                    direction = -1
                    current_turn = player_order[-1]
                    last_action = (
                        f"Game started – {start_card} reverses direction, "
                        f"Player {current_turn} goes first"
                    )
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
                "player_order": player_order,
                "direction": direction,
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

        lines: list[str] = []
        lines.append("=== Your Hand ===")
        for i, card in enumerate(hand, 1):
            lines.append(f" {i}. {card}")

        lines.append("")
        lines.append("=== Table ===")
        lines.append(f"Top card: {top_card}")
        lines.append(f"Current color: {current_color}")
        lines.append(f"Draw pile: {draw_count} cards")

        # Show opponent card counts
        is_2p = len(state["player_order"]) == 2
        for pid in state["player_order"]:
            if pid != self.player:
                count = len(state["hands"][pid])
                if is_2p:
                    lines.append(f"Opponent has: {count} cards")
                else:
                    lines.append(f"Player {pid} has: {count} cards")

        # Show direction for 3+ player games
        if not is_2p:
            dir_label = "Clockwise" if state["direction"] == 1 else "Counter-clockwise"
            lines.append(f"Direction: {dir_label}")

        lines.append("")
        winner = state["winner"]
        if winner == self.player:
            lines.append("Status: YOU WON!")
        elif winner is not None:
            if is_2p:
                lines.append("Status: OPPONENT WON!")
            else:
                lines.append(f"Status: Player {winner} WON!")
        elif state["current_turn"] == self.player:
            lines.append("Status: YOUR TURN")
        else:
            if is_2p:
                lines.append("Status: OPPONENT'S TURN")
            else:
                lines.append(f"Status: Player {state['current_turn']}'s TURN")

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
            msg = f"You played {card}."

            if card_type == "Skip":
                skipped = self._next_player(state, self.player)
                state["current_turn"] = self._next_player(state, skipped)
                msg += f" {skipped} is skipped."
            elif card_type == "Reverse":
                if len(state["player_order"]) == 2:
                    # 2-player: acts as Skip
                    state["current_turn"] = self.player
                    other = self._next_player(state, self.player)
                    msg += f" {other} is skipped."
                else:
                    # 3+ players: reverse direction, next player goes
                    state["direction"] *= -1
                    state["current_turn"] = self._next_player(state, self.player)
                    dir_label = "Clockwise" if state["direction"] == 1 else "Counter-clockwise"
                    msg += f" Direction is now {dir_label}."
            elif card_type == "Draw Two":
                reshuffle_if_needed(state)
                victim = self._next_player(state, self.player)
                victim_hand = state["hands"][victim]
                for _ in range(2):
                    if state["draw_pile"]:
                        victim_hand.append(state["draw_pile"].pop())
                state["current_turn"] = self._next_player(state, victim)
                msg += f" {victim} draws 2 and is skipped."
            elif card == "Wild Draw Four":
                reshuffle_if_needed(state)
                victim = self._next_player(state, self.player)
                victim_hand = state["hands"][victim]
                for _ in range(4):
                    if state["draw_pile"]:
                        victim_hand.append(state["draw_pile"].pop())
                state["current_turn"] = self._next_player(state, victim)
                msg += (
                    f" Color is now {chosen_color}. "
                    f"{victim} draws 4 and is skipped."
                )
            elif card == "Wild":
                state["current_turn"] = self._next_player(state, self.player)
                msg += f" Color is now {chosen_color}."
            else:
                # Normal number card
                state["current_turn"] = self._next_player(state, self.player)

            # Check win (after effects applied)
            if len(hand) == 0:
                state["winner"] = self.player
                state["last_action"] = (
                    f"Player {self.player} played {card} and won!"
                )
                await self._save_state(state)
                await self.redis.publish(self._pub_channel, "update")
                return f"You played {card}. You win!"

            state["last_action"] = (
                f"Player {self.player} played {card}"
                + (f" (chose {chosen_color})" if wild else "")
            )
            await self._save_state(state)
            await self.redis.publish(self._pub_channel, "update")
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
            state["current_turn"] = self._next_player(state, self.player)
            state["last_action"] = f"Player {self.player} drew a card"
            await self._save_state(state)
            await self.redis.publish(self._pub_channel, "update")
            return f"You drew: {drawn}"
        finally:
            await self._release_lock()

    async def wait(self, timeout: float = 60.0) -> str:
        pubsub = self.redis.pubsub()
        try:
            await pubsub.subscribe(self._pub_channel)
            # Subscribe-before-check to avoid race conditions
            state = await self.get_state()
            if state["winner"] or state["current_turn"] == self.player:
                return state["last_action"]
            # Wait for notifications
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise ValueError("Timed out waiting for your turn.")
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=min(remaining, 1.0),
                )
                if msg is not None:
                    state = await self.get_state()
                    if state["winner"] or state["current_turn"] == self.player:
                        return state["last_action"]
        finally:
            await pubsub.unsubscribe(self._pub_channel)
            await pubsub.close()


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------
server = Server("uno")
game: UnoGame | None = None

# Lobby-mode state
lobby_mode = False
ai_tasks: list[asyncio.Task] = []
ai_games: list[UnoGame] = []


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
        types.Tool(
            name="wait",
            description="Block until it is your turn. Returns the last action.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Max seconds to wait (default 60).",
                    }
                },
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
    elif name == "wait":
        timeout = arguments.get("timeout", 60.0)
        result = await game.wait(timeout)
    else:
        raise ValueError(f"Unknown tool: {name}")
    return [types.TextContent(type="text", text=result)]


# ---------------------------------------------------------------------------
# Web server — renders game state as auto-refreshing HTML
# ---------------------------------------------------------------------------
def _card_css_color(card: str) -> str:
    """Return a CSS color string for a card."""
    if card.startswith("Red"):
        return "#e74c3c"
    if card.startswith("Yellow"):
        return "#f1c40f"
    if card.startswith("Green"):
        return "#2ecc71"
    if card.startswith("Blue"):
        return "#3498db"
    return "#555"  # wild


async def lobby_handler(request):
    """Render the lobby page where the user picks player count."""
    html_content = """<!DOCTYPE html>
<html><head><title>UNO - Lobby</title>
<style>
body { font-family: 'Segoe UI', monospace; padding: 2em; background: #1a1a2e; color: #e0e0e0; text-align: center; }
h1 { font-size: 48px; margin-bottom: 8px; }
.subtitle { color: #aaa; margin-bottom: 40px; font-size: 18px; }
.buttons { display: flex; gap: 20px; justify-content: center; flex-wrap: wrap; }
.btn { padding: 24px 48px; border: none; border-radius: 12px; cursor: pointer; font-weight: bold; font-size: 20px; color: #fff; transition: transform 0.1s; }
.btn:hover { transform: scale(1.05); }
.btn:active { transform: scale(0.98); }
</style></head><body>
<h1>UNO</h1>
<div class="subtitle">Choose number of players to start a game</div>
<div class="buttons">
  <form method="post" action="/new-game">
    <input type="hidden" name="num_players" value="2">
    <button class="btn" style="background:#e74c3c;">2 Players</button>
  </form>
  <form method="post" action="/new-game">
    <input type="hidden" name="num_players" value="3">
    <button class="btn" style="background:#2ecc71;">3 Players</button>
  </form>
  <form method="post" action="/new-game">
    <input type="hidden" name="num_players" value="4">
    <button class="btn" style="background:#3498db;">4 Players</button>
  </form>
</div>
</body></html>"""
    return web.Response(text=html_content, content_type="text/html")


async def new_game_handler(request):
    """Start a new game from the lobby."""
    global game
    data = await request.post()
    num_players = int(data.get("num_players", 2))
    if num_players not in (2, 3, 4):
        num_players = 2

    # Stop any previous AI players
    await _stop_ai_players()

    # Clean up previous game
    if game is not None:
        # Delete old Redis keys
        old_key = game._key
        old_lock = game._lock_key
        r = game.redis
        if r:
            await r.delete(old_key, old_lock)
        await game.close()
        game = None

    game_id = uuid.uuid4().hex[:8]
    game = UnoGame(game_id, "A", num_players)
    await game.initialize()

    await _start_ai_players(game_id, num_players)

    raise web.HTTPFound("/")


async def end_game_handler(request):
    """End the current game and return to lobby."""
    global game
    await _stop_ai_players()
    if game is not None:
        r = game.redis
        if r:
            await r.delete(game._key, game._lock_key)
        await game.close()
        game = None
    raise web.HTTPFound("/")


async def web_handler(request):
    if lobby_mode and game is None:
        return await lobby_handler(request)
    if game is None:
        raise web.HTTPFound("/")
    state = await game.get_state()
    hand = state["hands"][game.player]
    top_card = state["discard_pile"][-1]
    current_color = state["current_color"]
    draw_count = len(state["draw_pile"])
    my_turn = state["current_turn"] == game.player
    winner = state["winner"]
    is_2p = len(state["player_order"]) == 2

    auto = request.query.get("auto", "0") == "1"

    msg = urllib.parse.unquote(request.query.get("msg", ""))
    err = urllib.parse.unquote(request.query.get("err", ""))

    # Flash banner
    flash = ""
    if err:
        flash = f'<div style="background:#c0392b;color:#fff;padding:10px 16px;border-radius:6px;margin-bottom:16px;">{html_module.escape(err)}</div>'
    elif msg:
        flash = f'<div style="background:#27ae60;color:#fff;padding:10px 16px;border-radius:6px;margin-bottom:16px;">{html_module.escape(msg)}</div>'

    # Status line
    if winner == game.player:
        status_line = "YOU WON!"
    elif winner is not None:
        status_line = f"Player {winner} WON!" if not is_2p else "OPPONENT WON!"
    elif my_turn:
        status_line = "YOUR TURN"
    else:
        who = f"Player {state['current_turn']}'s TURN" if not is_2p else "OPPONENT'S TURN"
        status_line = who

    # Opponents
    opponents_html = ""
    for pid in state["player_order"]:
        if pid != game.player:
            count = len(state["hands"][pid])
            label = f"Player {pid}" if not is_2p else "Opponent"
            opponents_html += f"<div>{label}: {count} cards</div>"

    # Direction (3+ players)
    dir_html = ""
    if not is_2p:
        dir_label = "Clockwise" if state["direction"] == 1 else "Counter-clockwise"
        dir_html = f"<div>Direction: {dir_label}</div>"

    # Last action
    last_action = html_module.escape(state.get("last_action", ""))

    # Card buttons
    color_btn_style = "padding:6px 14px;border:none;border-radius:4px;cursor:pointer;font-weight:bold;font-size:14px;color:#fff;margin:2px;"
    color_map = {"Red": "#e74c3c", "Yellow": "#f1c40f", "Green": "#2ecc71", "Blue": "#3498db"}

    cards_html = ""
    for card in hand:
        c_esc = html_module.escape(card)
        bg = _card_css_color(card)
        if is_wild(card):
            # Wild card: show card name + 4 color buttons
            cards_html += f'<div style="display:inline-block;background:#333;border-radius:8px;padding:8px;margin:4px;vertical-align:top;text-align:center;">'
            cards_html += f'<div style="font-weight:bold;margin-bottom:6px;color:#ccc;">{c_esc}</div>'
            for color_name, color_hex in color_map.items():
                disabled = "" if my_turn and not winner else " disabled"
                cards_html += (
                    f'<form method="post" action="/play" style="display:inline;">'
                    f'<input type="hidden" name="card" value="{c_esc}">'
                    f'<input type="hidden" name="chosen_color" value="{color_name}">'
                    f'<button type="submit" style="{color_btn_style}background:{color_hex};"{disabled}>{color_name[0]}</button>'
                    f'</form>'
                )
            cards_html += '</div>'
        else:
            disabled = "" if my_turn and not winner else " disabled"
            cards_html += (
                f'<form method="post" action="/play" style="display:inline;">'
                f'<input type="hidden" name="card" value="{c_esc}">'
                f'<button type="submit" style="padding:10px 16px;border:2px solid #555;border-radius:8px;'
                f'cursor:pointer;font-weight:bold;font-size:15px;color:#fff;margin:4px;background:{bg};"{disabled}>{c_esc}</button>'
                f'</form>'
            )

    # Draw button
    draw_disabled = "" if my_turn and not winner else " disabled"
    draw_html = (
        f'<form method="post" action="/draw" style="margin-top:12px;">'
        f'<button type="submit" style="padding:12px 28px;border:2px solid #888;border-radius:8px;'
        f'cursor:pointer;font-size:16px;font-weight:bold;color:#fff;background:#444;"{draw_disabled}>Draw Card</button>'
        f'</form>'
    )

    # Top card display
    top_bg = _card_css_color(top_card)
    top_esc = html_module.escape(top_card)
    color_esc = html_module.escape(current_color)

    # Lobby-mode buttons for game over
    lobby_buttons = ""
    if lobby_mode and winner:
        lobby_buttons = (
            '<div style="margin-top:20px;display:flex;gap:12px;">'
            '<form method="post" action="/new-game">'
            f'<input type="hidden" name="num_players" value="{len(state["player_order"])}">'
            '<button type="submit" style="padding:12px 28px;border:none;border-radius:8px;cursor:pointer;font-size:16px;font-weight:bold;color:#fff;background:#27ae60;">New Game</button>'
            '</form>'
            '<form method="post" action="/end-game">'
            '<button type="submit" style="padding:12px 28px;border:none;border-radius:8px;cursor:pointer;font-size:16px;font-weight:bold;color:#fff;background:#555;">Back to Lobby</button>'
            '</form>'
            '</div>'
        )

    # Auto-refresh / auto-play logic
    refresh_js = ""
    if auto and not winner:
        if my_turn:
            # Auto-submit the /auto form after 1 second
            refresh_js = '<script>setTimeout(()=>document.getElementById("auto-form").submit(),1000);</script>'
        else:
            # Keep refreshing to detect turn change
            refresh_js = '<script>setTimeout(()=>location.replace("/?auto=1"),2000);</script>'
    elif not my_turn and not winner:
        refresh_js = "<script>setTimeout(()=>location.replace(location.pathname),2000);</script>"

    html_content = f"""<!DOCTYPE html>
<html><head><title>UNO - Player {game.player}</title>
<style>
body {{ font-family: 'Segoe UI', monospace; padding: 1.5em; background: #1a1a2e; color: #e0e0e0; font-size: 16px; margin: 0 auto; max-width: 900px; }}
h2 {{ margin: 0.3em 0; }}
.table {{ background: #16213e; padding: 16px; border-radius: 10px; margin: 12px 0; display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }}
.top-card {{ display:inline-block; padding:14px 22px; border-radius:10px; font-weight:bold; font-size:18px; color:#fff; background:{top_bg}; border: 3px solid #fff3; }}
.status {{ font-size: 20px; font-weight: bold; margin: 10px 0; }}
</style>
{refresh_js}
</head><body>
{flash}
<h2>UNO &mdash; Player {game.player}</h2>
<div style="display:flex;align-items:center;gap:12px;margin:8px 0;">
  <div class="status">{html_module.escape(status_line)}</div>
  {'<span style="background:#e74c3c;color:#fff;padding:4px 10px;border-radius:4px;font-weight:bold;font-size:13px;">AUTO MODE: ON</span>' if auto else ''}
  <a href="/?auto={'0' if auto else '1'}" style="padding:6px 14px;border-radius:4px;background:{'#c0392b' if auto else '#27ae60'};color:#fff;text-decoration:none;font-weight:bold;font-size:13px;">{'Stop Auto' if auto else 'Start Auto'}</a>
</div>
<form id="auto-form" method="post" action="/auto" style="display:none;"></form>
<div style="color:#aaa;font-size:14px;margin-bottom:8px;">{last_action}</div>

<div class="table">
  <div>Top card: <span class="top-card">{top_esc}</span></div>
  <div>Current color: <span style="color:{color_map.get(current_color, '#ccc')};font-weight:bold;">{color_esc}</span></div>
  <div>Draw pile: {draw_count} cards</div>
  {opponents_html}
  {dir_html}
</div>

<h3>Your Hand ({len(hand)} cards)</h3>
<div>{cards_html}</div>
{draw_html}
{lobby_buttons}
</body></html>"""
    return web.Response(text=html_content, content_type="text/html")


async def play_handler(request):
    if game is None:
        raise web.HTTPFound("/")

    data = await request.post()
    card = data.get("card", "")
    chosen_color = data.get("chosen_color") or None
    try:
        result = await game.play(card, chosen_color)
        raise web.HTTPFound(f"/?msg={urllib.parse.quote(result)}")
    except ValueError as e:
        raise web.HTTPFound(f"/?err={urllib.parse.quote(str(e))}")


async def draw_handler(request):
    if game is None:
        raise web.HTTPFound("/")

    try:
        result = await game.draw()
        raise web.HTTPFound(f"/?msg={urllib.parse.quote(result)}")
    except ValueError as e:
        raise web.HTTPFound(f"/?err={urllib.parse.quote(str(e))}")


def _fallback_move(hand, top_card, current_color):
    """Pick the first valid card (or draw). Returns (action, card, chosen_color)."""
    for card in hand:
        if is_valid_play(card, top_card, current_color):
            chosen_color = None
            if is_wild(card):
                chosen_color = random.choice(COLORS)
            return "play", card, chosen_color
    return "draw", None, None


async def _ai_move(ai_game: UnoGame) -> None:
    """Make one AI move (LLM with fallback) for the given game instance."""
    state = await ai_game.get_state()
    hand = state["hands"][ai_game.player]
    top_card = state["discard_pile"][-1]
    current_color = state["current_color"]

    if state["winner"] or state["current_turn"] != ai_game.player:
        return

    action, card, chosen_color = "draw", None, None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if api_key:
        opponents = []
        for pid in state["player_order"]:
            if pid != ai_game.player:
                opponents.append(f"Player {pid}: {len(state['hands'][pid])} cards")
        user_msg = (
            f"Your hand: {', '.join(hand)}\n"
            f"Top card: {top_card}\n"
            f"Current color: {current_color}\n"
            f"Opponents: {'; '.join(opponents)}"
        )
        try:
            client = anthropic.AsyncAnthropic()
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                system=(
                    "You are playing UNO. Given the game state, choose your move. "
                    "Reply with ONLY JSON: "
                    '{\"action\":\"play\",\"card\":\"<card>\",\"chosen_color\":\"<Color or null>\"} '
                    'or {\"action\":\"draw\"}. '
                    "For Wild cards, chosen_color must be Red/Yellow/Green/Blue. "
                    "Pick strategically — match colors you have many of."
                ),
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = resp.content[0].text.strip()
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            move = json.loads(raw)
            action = move.get("action", "draw")
            if action == "play":
                card = move.get("card")
                chosen_color = move.get("chosen_color")
                if chosen_color == "null" or chosen_color is None:
                    chosen_color = None
                if card not in hand or not is_valid_play(card, top_card, current_color):
                    action, card, chosen_color = _fallback_move(hand, top_card, current_color)
                elif is_wild(card) and chosen_color not in COLORS:
                    chosen_color = random.choice(COLORS)
        except Exception:
            action, card, chosen_color = _fallback_move(hand, top_card, current_color)
    else:
        action, card, chosen_color = _fallback_move(hand, top_card, current_color)

    if action == "play" and card:
        await ai_game.play(card, chosen_color)
    else:
        await ai_game.draw()


async def _ai_loop(ai_game: UnoGame) -> None:
    """Background loop: wait for turn, make a move, repeat until game over."""
    try:
        while True:
            await ai_game.wait(timeout=120.0)
            state = await ai_game.get_state()
            if state["winner"]:
                return
            if state["current_turn"] != ai_game.player:
                continue
            await asyncio.sleep(0.8)
            await _ai_move(ai_game)
    except asyncio.CancelledError:
        return
    except Exception:
        return


async def _start_ai_players(game_id: str, num_players: int) -> None:
    """Create UnoGame instances for AI players and start their loops."""
    global ai_tasks, ai_games
    players = ["B", "C", "D"][: num_players - 1]
    for pid in players:
        ai_game = UnoGame(game_id, pid, num_players)
        await ai_game.initialize()
        ai_games.append(ai_game)
        task = asyncio.create_task(_ai_loop(ai_game))
        ai_tasks.append(task)


async def _stop_ai_players() -> None:
    """Cancel all AI tasks and close their game instances."""
    global ai_tasks, ai_games
    for task in ai_tasks:
        task.cancel()
    for task in ai_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    for ai_game in ai_games:
        await ai_game.close()
    ai_tasks = []
    ai_games = []


async def auto_handler(request):
    if game is None:
        raise web.HTTPFound("/")
    state = await game.get_state()

    if state["winner"] or state["current_turn"] != game.player:
        raise web.HTTPFound("/?auto=1")

    try:
        await _ai_move(game)
        raise web.HTTPFound("/?auto=1&msg=" + urllib.parse.quote("Auto move played."))
    except ValueError as e:
        raise web.HTTPFound(f"/?auto=1&err={urllib.parse.quote(str(e))}")


async def main():
    global game, lobby_mode

    parser = argparse.ArgumentParser(description="UNO MCP Server")
    parser.add_argument("--game", default=None, help="Game ID (omit for lobby mode)")
    parser.add_argument(
        "--player", default=None, choices=["A", "B", "C", "D"],
        help="Player (A-D, omit for lobby mode)",
    )
    parser.add_argument(
        "--num-players", type=int, default=2, choices=[2, 3, 4],
        help="Number of players (default: 2)",
    )
    args = parser.parse_args()

    # Determine mode
    if args.game and args.player:
        # Legacy mode: MCP stdio + web server for one player
        lobby_mode = False
        game = UnoGame(args.game, args.player, args.num_players)
        await game.initialize()

        port = PORT_MAP[args.player]
        app = web.Application()
        app.router.add_get("/", web_handler)
        app.router.add_post("/play", play_handler)
        app.router.add_post("/draw", draw_handler)
        app.router.add_post("/auto", auto_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", port)
        await site.start()

        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )
        finally:
            await runner.cleanup()
            await game.close()
    else:
        # Lobby mode: web-only, no MCP stdio
        lobby_mode = True
        game = None

        app = web.Application()
        app.router.add_get("/", web_handler)
        app.router.add_post("/play", play_handler)
        app.router.add_post("/draw", draw_handler)
        app.router.add_post("/auto", auto_handler)
        app.router.add_post("/new-game", new_game_handler)
        app.router.add_post("/end-game", end_game_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 19000)
        await site.start()

        print("UNO lobby running at http://localhost:19000/")
        try:
            await asyncio.Event().wait()
        finally:
            await _stop_ai_players()
            if game is not None:
                await game.close()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
