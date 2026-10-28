"""The shape every list tool's result shares: compact rows, an honest total.

A tool result is read by the model, and the model's context is the scarce
resource. So a list tool returns only the fields the prompt needs for each row,
never more rows than its cap, and always says how many there were — the model
can then answer "how many" from `total` and say "showing 200 of 1,340" rather
than presenting a capped list as the whole (hard rule 7).

Two things can make a result partial, and `truncation_note` names which:

- the **row cap** (`@tool(max_rows=...)` or `TOOL_MAX_ROWS`): `total` is exact
  and the rows are the front of the sorted list;
- the **vendor page cap** (`ConnectorLimits.max_pages`): the connector stopped
  reading, so `total` is a lower bound.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from app.agent.registry import limit_rows


class SiteTagged(BaseModel):
    """Every row says which end customer it belongs to."""

    site: str = Field(description="The end customer's name.")
    site_slug: str = Field(description="The end customer's slug, for asking about it again.")


class RowCapped(BaseModel):
    """The counting half of a list result."""

    total: int = Field(description="How many rows matched, before the row cap.")
    returned: int = Field(description="How many rows are in this result.")
    truncated: bool = Field(
        default=False,
        description="True when rows were left out, by the row cap or the vendor page cap.",
    )
    truncation_note: str | None = Field(
        default=None,
        description="Why the result is partial and how to see the rest. Repeat it to the user.",
    )


def capped[T](
    tool_name: str,
    rows: Sequence[T],
    *,
    noun: str = "rows",
    page_truncated: bool = False,
) -> tuple[list[T], dict[str, Any]]:
    """Cap `rows` for `tool_name` and build the `RowCapped` fields to go with them.

    `rows` must already be sorted the way the answer should read, because the
    cap keeps the front. `page_truncated` is the connector's own flag.
    """
    window = limit_rows(tool_name, rows)
    notes: list[str] = []
    if window.truncated:
        notes.append(
            f"Showing the first {len(window.rows)} of {window.total} {noun}. "
            "Say so in the answer; narrow the question or use a count tool for the rest."
        )
    if page_truncated:
        notes.append(
            "The vendor listing stopped at the page cap, so the total is a lower bound "
            "and the list is incomplete. Say so in the answer."
        )
    return window.rows, {
        "total": window.total,
        "returned": len(window.rows),
        "truncated": window.truncated or page_truncated,
        "truncation_note": " ".join(notes) or None,
    }
