"""Generic cursor-paginated response."""

from __future__ import annotations

from pydantic import BaseModel, Field

# Page sizes are bounded so a client cannot ask for an unbounded result set.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class Page[T](BaseModel):
    """One page of results plus the cursor that fetches the next.

    ``next_cursor`` is null exactly when there is nothing more to read, so a
    client loops until it is null rather than comparing counts to a page size.
    """

    items: list[T]
    next_cursor: str | None = Field(default=None)
    has_more: bool = False
