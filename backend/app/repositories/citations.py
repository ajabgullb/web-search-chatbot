"""Citation queries.

Only ever read through message ids the caller already obtained from an
ownership-checked query, so no user scoping is repeated here.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.citation import Citation


class CitationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add_all(self, citations: Iterable[Citation]) -> None:
        self._session.add_all(list(citations))

    async def for_messages(
        self, message_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[Citation]]:
        """Citations for a page of messages, in one query, ordered by ordinal."""
        if not message_ids:
            return {}
        result = await self._session.execute(
            select(Citation)
            .where(Citation.message_id.in_(message_ids))
            .order_by(Citation.message_id, Citation.ordinal)
        )
        grouped: dict[uuid.UUID, list[Citation]] = defaultdict(list)
        for citation in result.scalars():
            grouped[citation.message_id].append(citation)
        return dict(grouped)
