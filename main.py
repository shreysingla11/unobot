import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequest, ListToolsRequest


class UnoGame:
    pass

# Global game instance
game = UnoGame()

# Create MCP server
server = Server("uno")


# Implement MCP server methods.
@server.list_tools()
async def list_game_commands(request: ListToolsRequest):
    raise NotImplementedError()


@server.call_tool()
async def handle_command(request: CallToolRequest):
    raise NotImplementedError()


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
