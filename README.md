# MCP Project
In this project you will be developing an M(odel)C(ontext)P(rotocol) server to allow
an LLM to interact with a two player game of UNO. The question has multiple parts and you should
solve each part before progressing to the next one. There is no golden answer or
specific check that must be passed at each part but each part has a sensible goal and
you should be able to judge if your solution meets the goal.

Start here: https://github.com/modelcontextprotocol to learn about
the Model Context Protocol. Only look at the Python SDK since this project
is based on the Python MCP SDK.

## Instructions
You have been provided with:
- This initial repository that has instructions for setting up a working environment (see below)
- An API key that can be used to access the Anthropic API

This project requires at least Python 3.11, ensure that the Python interpreter
you are using meets this requirement.

It is recommended you setup a venv to install the requirements for this project,
though that is not required.

Setup requires installing the sources in this project as editable modules.
You should be able to just run this command to get started.

```shell
pip install -e . mcp-use
```

The problem requires storing persistent state in a storage system. You can use the file
system as your persistent store where it is required. However, it is preferable to use
Redis to store the game state. This requires installing Redis in your environment.
Instructions are available at:
https://redis.io/docs/latest/operate/oss_and_stack/install/archive/install-redis/

You will also need to install the corresponding python library:

```shell
pip install redis
```


Implement the following functionality in order:

## Problem

### Part 1: MCP Stdio Server

Implement an MCP server that manages the state for playing a two-player UNO card game.
If you are not familiar with the game you can read about it here:
https://en.wikipedia.org/wiki/Uno_(card_game)

The game uses a standard UNO deck of 108 cards:
- Number cards (0-9) in four colors: Red, Yellow, Green, Blue
  - One 0 per color, two of each 1-9 per color
- Action cards in four colors: Skip, Reverse, Draw Two
  - Two of each per color
- Wild cards: Wild (4), Wild Draw Four (4)

Note: In a 2-player game, Reverse acts the same as Skip.

Implement the MCP game server in `main.py`. The server should be a stdio server and
there is a simple starting file provided which you need to complete with the
game implementation. You should be able to test your game implementation directly
by piping JSON requests to it:

```shell
echo <Some JSON> | python main.py --game=test_game --player=A
```

The game is a two player game. You will support this by having your MCP server accept
2 command line arguments:
- Game ID: Any unique string
- Player: One of {A, B}

When a new game starts, the deck is shuffled, each player is dealt 7 cards, and the
top card of the remaining deck is placed face up to start the discard pile.
Player A gets the first turn.

The outline of game play is as follows:
1. On their turn, a player either:
   - Plays a valid card from their hand (eg. "Red 5", "Wild" choosing color "Blue")
   - Draws a card from the draw pile if they cannot or choose not to play
2. The game continues until one player empties their hand

You should support the following MCP tools:
- Status: display the current state of the game
- Play: play a card from the player's hand
- Draw: draw a card from the draw pile

Play should respond with the outcome of the action. If it is not the player's turn
return an error indicating it is not their turn yet.

Status should produce output like:
```
=== Your Hand ===
 1. Red 3
 2. Red Skip
 3. Blue 7
 4. Green 2
 5. Wild

=== Table ===
Top card: Red 5
Current color: Red
Draw pile: 42 cards
Opponent has: 4 cards

Status: YOUR TURN
```

Status returns the player's hand, the table state (top of discard pile, active color,
draw pile size), the number of cards in the opponent's hand (not what they are),
and indicates whose turn it is or if one of the players has won the game.

IMPORTANT: Implement this as an MCP stdio server (not an HTTP server). You **MUST** implement
the server using the MCP SDK that is available in the `mcp` python package. You can learn
about the SDK by looking at its git repo at https://github.com/modelcontextprotocol/python-sdk.
Do not implement the MCP wire protocol directly.

Keep in mind that there are 2 separate processes interacting to play a game so you must
design a mechanism for the processes to communicate the state of the game between themselves.
For example they could both look at a file whose name is derived from the game ID. Ideally,
you would use Redis to store the state of the game and coordinate the interaction between the processes.
It should be possible to kill a process and then relaunch it to resume a game.

### Part 2

Create a testing script that simulates a full sequence of interactivity with a pair
of MCP server processes you just implemented. Exercise all the functions:
- Play several rounds of the game
- Print out all the interactions
- Display the final game state
- Check that the end state matches expectations
- Create a script `run_test.sh` that can be executed to run your test.

The test script can be as simple as:
```shell
python test.py
```
if you implement the test by completing `test.py`.

Keep in mind it will have to interact with 2 processes, one for each player.

### Part 3

Now you are going add a new tool to your MCP server to allow for more real time
coordination between the two MCP servers. Currently, there is no way for a player
to detect when it is their turn to play other than repeatedly calling `Status`
till it reports it is their turn. Additionally, they will have to infer their
opponent's move based on the change in the response of Status.

You are going to implement a new tool `Wait`. This tool blocks until it is the
player's turn to play. It responds with the most recent move made by the opponent.
If it is already the player's turn to play it immediately returns with the recent
move information. This requires that the two processes synchronize using the
persistent store so that each process can detect when the other has made a state
change. Implement this using any approach you think is reasonable but a good
approach would both be low latency and efficient.

Create a new version of your test (`run_wait_test.sh`) that leverages this waiting
operation to simulate game play between two players.

### Part 4: Implement an LLM Player

Now you are going to use your MCP server and prompt the LLM so that the LLM plays
as an opponent.

```shell
ANTHROPIC_API_KEY=<key> python python/chat.py --game-id=<identifier>
```

Modify `chat.py` so that the LLM plays as player B of this game.
- The script is setup to use Claude Haiku as the LLM
- Modify the implementation and system prompt so that it plays the full game.
- Commit a script `llm_play.sh` that gives an example invocation.

### Part 5: Bonus work

Extend your MCP server to support 3-4 players. The server should accept a player
identifier from {A, B, C, D} and support games with a variable number of participants.
With more than 2 players, Reverse now changes the direction of play rather than acting
as Skip. Update your Status tool to show the card count for each opponent.

Additionally, add a web server to your MCP server implementation that renders a view of the
current game state. You should be able to navigate to `http://localhost:19000/` and see the
current state of the game (ie Status) as would be seen by the player associated
with the instance of the server. You can default to port 19000 for player A, 19001 for player B,
and so on.

## Documenting your work

To aid in judging your work it is important to record the details of how you approached the problem:
- The documentation and tools you used to develop the solution
- Any command lines or scripts you used for debugging and testing your work
- Logs of commands you ran indicating that things were working
- Multiple commits capturing your incremental progress
It is easiest to share all of these through the repo, feel free to commit markdown files into the repo recording this information. Similarly, add temporary scripts and logs to the repository so we can review them.

*NOTE: Limit your solution to a Python implementation. Do not try to use other languages. Shell scripts are ok for documenting python invocations.*

## Expected Outcome
At the end of your task all of your work should be committed to the repository provided. Include a `SOLUTION.md` in one of your commits explaining the tests added and instructions on how to run and any scripts you created. `SOLUTION.md` should also explain in detail how you used any AI tools to support your work and give some indication of what code was written with the assistance of AI. Ideally, your commit messages will reflect if a substantial portion of the commit was AI generated.

## Evaluation Criteria

| Criteria | Description |
|----------|-------------|
| Working scripts | Scripts that can run the implemented code |
| Use of AI | Effective use of AI to achieve the goal |
| Quality of code | Subjective assessment of implementation |
| Documentation | Description of implementation |
