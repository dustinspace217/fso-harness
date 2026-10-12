"""Exit codes and the structured error every fsoctl verb raises.

WHY codes: the MCP wrapper and agents branch on the code (timeout vs engine dead vs refused) without
parsing message text; the hint tells the agent the next thing to try.
"""
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_LAUNCH = 4
EXIT_TIMEOUT = 5
EXIT_ENGINE = 6
EXIT_REFUSED = 7
# A Lua error inside the bridge or a script it ran (not a deliberate refusal). Kept apart from
# EXIT_REFUSED so an agent does not read "the harness broke" as "your input was invalid" (QA SF-18).
EXIT_BRIDGE = 8
# The verb ran but what it ran failed (a compile failed). The result keeps its diagnostics; the exit
# code lets shell automation (`fsoctl build X && fsoctl launch ...`) stop (cross-family X-3/XK-2).
EXIT_FAILED = 9


class HarnessError(Exception):
    """An fsoctl failure with an exit code, a message, and a next-step hint.

    Receives: code (one of the EXIT_* values above), message (what went wrong), hint (what to try).
    Raised by every verb; cli.main turns it into JSON/text output and the process exit code.
    """

    def __init__(self, code: int, message: str, hint: str = "",
                 details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}   # extra facts, e.g. {"id": session} for a failed launch

    def to_dict(self) -> dict:
        """Return the JSON shape used by --json output and the MCP wrapper."""
        out = {"ok": False, "code": self.code, "message": self.message, "hint": self.hint}
        if self.details:
            out["details"] = self.details
        return out
