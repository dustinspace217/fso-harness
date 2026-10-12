#!/usr/bin/env bash
# Register the fso-harness MCP server with Claude Code at USER scope (every project).
# Run setup.sh first. Remove again with: claude mcp remove --scope user fso-harness
#
# The harness's environment is baked into the registration with -e. WHY: Claude Code starts a
# stdio MCP server with its own process environment (read from the 2.1.296 binary), not with
# the shell you registered from. A session started from a desktop launcher or a service has no
# FS2PATH or FSO_BIN, and every tool would then fail with "FS2PATH is not set". Re-run this
# script after moving the game folder or the binaries; the old registration is replaced.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -x "${HERE}/.venv/bin/python" ] || { echo "run ${HERE}/setup.sh first"; exit 1; }

missing=""
[ -n "${FS2PATH:-}" ] || missing="${missing} FS2PATH"
[ -n "${FSO_BIN:-}" ] || missing="${missing} FSO_BIN"
if [ -n "${missing}" ]; then
	echo "not registered: export${missing} first (the registration stores them)" >&2
	exit 2
fi

envargs=(-e "FS2PATH=${FS2PATH}" -e "FSO_BIN=${FSO_BIN}")
[ -n "${FSO_BIN_DEBUG:-}" ] && envargs+=(-e "FSO_BIN_DEBUG=${FSO_BIN_DEBUG}")
[ -n "${QTFRED_BIN:-}" ] && envargs+=(-e "QTFRED_BIN=${QTFRED_BIN}")

# Replace an existing registration so a re-run with new paths does not fail on "already exists".
claude mcp remove --scope user fso-harness >/dev/null 2>&1 || true
# The name goes BEFORE the -e list: -e takes any number of values, so a name placed after it
# is read as one more KEY=value and rejected ("Invalid environment variable format").
claude mcp add --scope user fso-harness "${envargs[@]}" -- "${HERE}/.venv/bin/python" "${HERE}/server.py"
echo "registered with FS2PATH=${FS2PATH} FSO_BIN=${FSO_BIN}" \
	"${FSO_BIN_DEBUG:+FSO_BIN_DEBUG=${FSO_BIN_DEBUG}} ${QTFRED_BIN:+QTFRED_BIN=${QTFRED_BIN}}"
echo "Restart Claude Code to pick it up; check with: claude mcp list"
