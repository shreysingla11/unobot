"""LLM UNO Player (Part 4).

Connects Claude Haiku to the UNO MCP server as Player B and plays a full game.

Usage:
    ANTHROPIC_API_KEY=<key> python python/chat.py --game-id=<identifier>
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from mcp_use import MCPAgent, MCPClient

SYSTEM_PROMPT = """\
You are playing a 2-player UNO card game as Player B. Play the full game to completion.

## UNO Rules
- Match cards by color OR number/type against the top discard card.
- Wild / Wild Draw Four can be played on anything — you MUST choose a color.
- Skip / Reverse (same in 2-player): opponent loses their turn.
- Draw Two: opponent draws 2 and loses their turn.
- Wild Draw Four: choose color, opponent draws 4 and loses their turn.
- If you cannot play, draw a card (ends your turn).
- First player to empty their hand wins.

## How to Play Each Turn
1. Call **wait** — blocks until it is your turn, returns opponent's last action.
2. Call **status** — shows your hand, top card, current color, whose turn it is.
3. Find a card in your hand that matches the current color OR the top card's number/type, or any Wild.
4. Call **play** with the EXACT card name from your hand (e.g. "Red 5", "Green Skip", "Wild Draw Four").
   - For Wild or Wild Draw Four you MUST also pass chosen_color (Red, Yellow, Green, or Blue).
   - Choose the color you have the most cards of.
5. If no card is playable, call **draw** instead.
6. After Skip/Reverse/Draw Two/Wild Draw Four you keep the turn — repeat from step 1.
7. Keep playing turns until someone wins.

## Important
- Use the EXACT card name as shown in status (e.g. "Red Draw Two", not "Red draw two").
- Do NOT play a card you do not have in your hand.
- Do NOT play when it is not your turn — call wait first.
- When the game ends (you see "YOU WON!" or "OPPONENT WON!" in status), stop playing.
"""

MAX_TURNS = 200


async def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="LLM UNO Player (Player B)")
    parser.add_argument("--game-id", required=True, help="Game ID to join")
    args = parser.parse_args()

    main_py = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "main.py")
    )

    config = {
        "mcpServers": {
            "uno": {
                "command": sys.executable,
                "args": [main_py, f"--game={args.game_id}", "--player=B"],
            }
        }
    }

    client = MCPClient.from_dict(config)
    llm = ChatAnthropic(model="claude-haiku-4-5-20251001")

    agent = MCPAgent(
        llm=llm,
        client=client,
        max_steps=10,
        system_prompt=SYSTEM_PROMPT,
        memory_enabled=True,
    )

    print(f"=== LLM Player B | Game: {args.game_id} ===\n", flush=True)

    try:
        for turn in range(1, MAX_TURNS + 1):
            if turn == 1:
                prompt = (
                    "The game has started. You are Player B. "
                    "Call wait to wait for your turn, then call status to see "
                    "the game state, then play a matching card or draw."
                )
            else:
                prompt = (
                    "Take your next turn: call wait, then status, "
                    "then play or draw."
                )

            print(f"--- Turn {turn} ---", flush=True)

            response = ""
            tool_called = False
            game_over = False
            async for chunk in agent.stream(prompt, max_steps=10):
                if isinstance(chunk, tuple):
                    action, observation = chunk
                    tool_called = True
                    obs_short = observation.replace("\n", " | ")
                    if len(obs_short) > 150:
                        obs_short = obs_short[:147] + "..."
                    print(f"  [{action.tool}] {obs_short}", flush=True)
                    # Check tool observations for game-over signals
                    obs_lower = observation.lower()
                    if "won!" in obs_lower or "you won" in obs_lower or "opponent won" in obs_lower:
                        game_over = True
                    if "game is already over" in obs_lower:
                        game_over = True
                elif isinstance(chunk, str):
                    response = chunk
                    print(f"  >> {response}", flush=True)

            print("", flush=True)

            # Check final response text for game-over keywords
            lower = response.lower()
            if any(
                kw in lower
                for kw in ["you win", "i win", "i won", "won!", "game over",
                           "game is already over", "opponent won", "you won"]
            ):
                game_over = True

            # Fallback: if the LLM didn't call any tools, the game is likely over
            # or the LLM is confused — either way, stop.
            if not tool_called:
                no_tool_turns = getattr(main, "_no_tool_turns", 0) + 1
                main._no_tool_turns = no_tool_turns
                if no_tool_turns >= 3:
                    print("  (LLM stopped using tools — ending)", flush=True)
                    game_over = True
            else:
                main._no_tool_turns = 0

            if game_over:
                print(f"\n=== Game over after {turn} turns ===", flush=True)
                break
        else:
            print(f"\n=== Reached {MAX_TURNS} turn limit ===", flush=True)
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
