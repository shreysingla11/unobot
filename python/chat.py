"""Simple command line to test MCP server implementation.

ANTHROPIC_API_KEY=<key> python chat.py

Expand this to test your agent MCP implementation. Extend it to manage
a command line conversation with the user.
"""

import asyncio
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from mcp_use import MCPAgent, MCPClient


async def main():
    load_dotenv()
    client = MCPClient.from_config_file("mcp-server-config.json")
    llm = ChatAnthropic(model="claude-3-5-haiku-20241022")
    agent = MCPAgent(llm=llm, client=client, max_steps=30)
    async for chunk in agent.stream("Let's play a game."):
        print(chunk, end="", flush=True)

if __name__ == "__main__":
    asyncio.run(main())