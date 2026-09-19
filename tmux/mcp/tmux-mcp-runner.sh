#!/bin/bash
THIS_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# tmux is local with no credentials, so there is nothing to configure here.
# The server inherits $TMUX / $TMUX_PANE from the Claude Code process that
# spawns it, which is how it scopes to the current session and pane.

exec uv run --with "mcp<2" python "$THIS_DIR/tmux-server.py"
