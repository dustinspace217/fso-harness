"""guide: return a section of docs/AGENT-GUIDE.md, or the list of its sections.

Why a verb (and an MCP tool) for a document: the MCP server's session-start INSTRUCTIONS are
capped at 2,048 characters and full, and only some guide sections are registered as prompts, so
an agent reached the rest only if it chose to open the file (2026-10-15: "Fly a mission", 8.6k
characters, was reachable no other way). Tool descriptions always load, so a `guide` tool makes
every section one call away without spending the INSTRUCTIONS budget.

The guide lives in the repository (docs/), not in the package: this verb works from a checkout,
which is how the harness is installed (editable install, mcp/register.sh), and says so when the
file is missing.
"""
import argparse
from pathlib import Path

from .cli import register
from .errors import EXIT_NOT_FOUND, EXIT_USAGE, HarnessError

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "AGENT-GUIDE.md"


def sections(text: str) -> list[str]:
    """Return the guide's `## ` headings in order (heading text only)."""
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def section_text(text: str, heading: str) -> str:
    """Return the `## heading` section, heading line included, up to the next `## ` heading."""
    lines = text.splitlines()
    start = lines.index(f"## {heading}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


def match(names: list[str], query: str) -> str:
    """Pick the heading `query` names: exact (any case) first, then a unique case-insensitive
    substring. Raises EXIT_USAGE for no match or an ambiguous one, listing the candidates."""
    q = query.strip().lower()
    exact = [n for n in names if n.lower() == q]
    if exact:
        return exact[0]
    hits = [n for n in names if q in n.lower()]
    if len(hits) == 1:
        return hits[0]
    what = "matches several sections" if hits else "matches no section"
    raise HarnessError(EXIT_USAGE, f"'{query}' {what}",
                       "sections: " + "; ".join(hits or names))


def _configure(p: argparse.ArgumentParser) -> None:
    """CLI: optional section name (exact heading or a unique part of one)."""
    p.add_argument("section", nargs="?", default=None,
                   help='section heading or a unique part of it, e.g. "fly"; omit to list them')


@register("guide", "read a section of the agent guide (omit the name to list sections)", _configure)
def _guide(args: argparse.Namespace) -> dict:
    """Return {"sections": [...]} without a name, else {"section": heading, "text": ...}."""
    if not GUIDE.is_file():
        raise HarnessError(EXIT_NOT_FOUND, f"agent guide not found at {GUIDE}",
                           "the guide ships in the repository; run the harness from a checkout")
    text = GUIDE.read_text()
    names = sections(text)
    if not args.section:
        return {"sections": names}
    heading = match(names, args.section)
    return {"section": heading, "text": section_text(text, heading)}
