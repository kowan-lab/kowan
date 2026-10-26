"""Table/formatting helpers.

Tables use a tabwriter-style column layout: each column is padded to the
widest cell it contains, plus two trailing spaces.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Sequence


def print_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Render a table with the given header and rows to stdout.

    Every column is as wide as its widest cell, followed by two padding spaces.
    Trailing whitespace on each line is stripped.
    """
    all_rows = [list(header)] + [list(r) for r in rows]
    ncols = len(header)
    widths = [0] * ncols
    for row in all_rows:
        for i in range(ncols):
            cell = row[i] if i < len(row) else ""
            # tabwriter measures display width in runes, not bytes.
            widths[i] = max(widths[i], len(cell))

    lines = []
    for row in all_rows:
        cells = []
        for i in range(ncols):
            cell = row[i] if i < len(row) else ""
            if i == ncols - 1:
                # Last column is not padded on the right.
                cells.append(cell)
            else:
                cells.append(cell.ljust(widths[i] + 2))
        lines.append("".join(cells).rstrip())
    sys.stdout.write("\n".join(lines) + "\n")


def truncate(s: str, n: int) -> str:
    """Shorten s to at most n characters, appending an ellipsis if truncated."""
    if len(s) <= n:
        return s
    if n <= 1:
        return s[:n]
    return s[: n - 1] + "…"


def short_id(id_: str) -> str:
    """Return at most the first 12 characters of an id (Docker-style)."""
    if id_.startswith("sha256:"):
        id_ = id_[len("sha256:") :]
    if len(id_) > 12:
        return id_[:12]
    return id_


def first_name(names: Sequence[str] | None) -> str:
    """First container name without its leading slash, or "<none>"."""
    if not names:
        return "<none>"
    name = names[0]
    if name.startswith("/"):
        return name[1:]
    return name


def first_tag(tags: Sequence[str] | None) -> str:
    """First repo tag, or "<none>"."""
    if not tags:
        return "<none>"
    return tags[0]


def human_bytes(n: int) -> str:
    """Format a byte count using binary (1024) units."""
    unit = 1024
    if n < unit:
        return f"{n} B"
    div, exp = unit, 0
    v = n // unit
    while v >= unit:
        div *= unit
        exp += 1
        v //= unit
    return f"{n / div:.1f} {'KMGTPE'[exp]}iB"


def unix_time(sec: int) -> str:
    """Format a Unix timestamp (seconds) as RFC3339, or "-" when zero."""
    if not sec:
        return "-"
    return (
        datetime.fromtimestamp(sec, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def dash(s: str | None) -> str:
    """Return "-" for an empty/blank string, otherwise the string itself."""
    if s is None or s.strip() == "":
        return "-"
    return s


def bool_str(b: bool) -> str:
    """Render a bool as a compact yes/no."""
    return "yes" if b else "no"
