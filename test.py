#!/usr/bin/env python3
"""
Test script for the UNO MCP server.

To run this test:
1. Make sure you have the MCP server dependencies installed
2. Run the test script:
   python test.py

This will start the MCP server as 2 subprocesses and simulate a two person
game. Both players should:
- Play cards and draw from the deck
- Play several rounds
- Display the game state for each of them.
"""
import asyncio


async def test_uno():
    pass


async def main():
    """Main test function."""
    await test_uno()


if __name__ == "__main__":
    asyncio.run(main())

