#!/usr/bin/env bash
# Create the MCP wrapper's isolated venv. fsoctl itself is stdlib-only; only this optional
# layer needs the `mcp` package, so it lives in mcp/.venv and nowhere else.
# Registering the server with Claude Code is a separate step (register.sh): it changes your
# user-level Claude Code configuration, so it is never done as a side effect of setup.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${HERE}/.venv"
if [ ! -d "${VENV}" ]; then
	echo "creating venv at ${VENV}"
	python3 -m venv "${VENV}"
fi
# mcp 2.0.0: the version this server was written and tested against (MCPServer, Image).
"${VENV}/bin/pip" install --quiet "mcp==2.0.0"
echo "venv ready. To make the tools available in Claude Code, run: bash ${HERE}/register.sh"
