"""Entry point for running as a module: ``python -m applemusic_mcp [command]``.

Dispatches through the CLI so every subcommand is reachable this way — notably
``helper`` and ``shim``, which the launchd job and your MCP client invoke.
Upstream ran the server unconditionally here, so a bare ``python -m
applemusic_mcp`` (with no arguments) still starts the stdio server; anything
else is handed to the CLI.
"""

import sys

if __name__ == "__main__":
    if len(sys.argv) == 1:
        from .server import main

        main()
    else:
        from .cli import main

        main()
