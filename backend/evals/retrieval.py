"""Live retrieval evaluation: real embeddings, real PostgreSQL, real selection.

    uv run python -m evals.retrieval --database-url postgresql+asyncpg://.../scratch

Costs money, very little: one batch call embeds every passage and another
every question in ``retrieval_dataset.json`` (about 3,000 tokens, roughly
$0.00006 with text-embedding-3-small). Needs ``EMBEDDING_API_KEY``, or an
``LLM_API_KEY`` for OpenAI, and a migrated scratch database: the data it
writes is removed again, but never point it at one you care about.

It answers the question the agent tests cannot: is ``DOCUMENT_MIN_SIMILARITY``
right for the real model? For each candidate floor it reports

- **recall**: answerable questions whose passage is among the chunks chosen;
- **leak**: unanswerable questions (near misses and off-topic) that still get
  a chunk, which is what invites a confident, wrong, cited answer.

The keyword half of hybrid search is reported separately, because a keyword
match is allowed past the floor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, text

from app.agent.context import AgentLimits
from app.agent.tools.documents import (
    ChunkCandidate,
    exact_terms,
    fuse,
    keyword_query,
    keyword_terms,
    select,
)
from app.core.config import Settings
from app.db.session import Database
from app.models import Conversation, Document, DocumentChunk, DocumentStatus, User
from app.providers.embeddings import OpenAIEmbeddings
from app.repositories.documents import DocumentRepository

DATASET = Path(__file__).with_name("retrieval_dataset.json")
# The eval user is never signed in to: no Argon2 hash can equal this.
UNUSABLE_HASH = "!unusable"

FLOORS = (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50)


async def _seed(
    database: Database, provider: OpenAIEmbeddings, passages: list[dict[str, Any]]
) -> tuple[uuid.UUID, uuid.UUID, dict[uuid.UUID, str]]:
    batch = await provider.embed([p["text"] for p in passages])
    async with database.open_session() as session:
        user = User(
            email=f"retrieval-eval-{uuid.uuid4().hex}@example.invalid",
            password_hash=UNUSABLE_HASH,
            display_name="Retrieval eval",
        )
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id, title="Retrieval eval")
        session.add(conversation)
        await session.flush()
        document = Document(
            user_id=user.id,
            conversation_id=conversation.id,
            filename="annual-report.pdf",
            content_sha256=uuid.uuid4().hex,
            byte_size=1,
            page_count=max(p["page"] for p in passages),
            char_count=sum(len(p["text"]) for p in passages),
            chunk_count=len(passages),
            status=DocumentStatus.ready,
            embedding_model=provider.model,
            embedding_dimensions=provider.dimensions,
        )
        session.add(document)
        await session.flush()
        ids: dict[uuid.UUID, str] = {}
        for ordinal, (passage, vector) in enumerate(zip(passages, batch.vectors, strict=True)):
            row = DocumentChunk(
                id=uuid.uuid4(),
                document_id=document.id,
                user_id=user.id,
                conversation_id=conversation.id,
                ordinal=ordinal,
                page=passage["page"],
                content=passage["text"],
                embedding=vector,
            )
            session.add(row)
            ids[row.id] = passage["id"]
        await session.commit()
    return user.id, conversation.id, ids


async def _candidates(
    database: Database,
    model: str,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    question: str,
    vector: list[float],
    limit: int,
) -> tuple[list[ChunkCandidate], list[ChunkCandidate]]:
    terms = keyword_terms(question)
    async with database.open_session() as session:
        repository = DocumentRepository(session)
        nearest = await repository.nearest_chunks(
            user_id=user_id,
            conversation_id=conversation_id,
            embedding_model=model,
            vector=vector,
            limit=limit,
        )
        matched = (
            await repository.keyword_chunks(
                user_id=user_id,
                conversation_id=conversation_id,
                embedding_model=model,
                vector=vector,
                tsquery=keyword_query(terms),
                limit=limit,
            )
            if terms
            else []
        )
    return nearest, matched


async def evaluate(database_url: str, floors: tuple[float, ...]) -> dict[str, Any]:
    settings = Settings(database_url=database_url)
    limits = AgentLimits.from_settings(settings)
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    database = Database(settings)
    provider = OpenAIEmbeddings(settings)
    try:
        async with database.open_session() as session:
            await session.execute(text("SELECT 1 FROM document_chunks LIMIT 1"))
        user_id, conversation_id, passage_of = await _seed(database, provider, dataset["passages"])
        try:
            questions = dataset["questions"]
            vectors = (await provider.embed([q["question"] for q in questions])).vectors
            rows = []
            for question, vector in zip(questions, vectors, strict=True):
                nearest, matched = await _candidates(
                    database,
                    provider.model,
                    user_id,
                    conversation_id,
                    question["question"],
                    vector,
                    limits.document_candidates,
                )
                rows.append((question, nearest, matched))
        finally:
            async with database.open_session() as session:
                # Cascades to the conversation, the document, and its chunks.
                await session.execute(delete(User).where(User.id == user_id))
                await session.commit()
    finally:
        await provider.aclose()
        await database.dispose()

    def chosen_ids(
        question: str,
        nearest: list[ChunkCandidate],
        matched: list[ChunkCandidate],
        floor: float,
        *,
        keywords: bool,
    ) -> list[str]:
        picked = select(
            fuse(nearest, matched if keywords else []),
            min_similarity=floor,
            top_k=limits.document_top_k,
            max_chars=limits.document_context_chars,
            exact=exact_terms(keyword_terms(question)) if keywords else (),
        )
        return [passage_of[r.candidate.chunk_id] for r in picked]

    answerable = [r for r in rows if r[0]["answer"] is not None]
    unanswerable = [r for r in rows if r[0]["answer"] is None]
    gold_similarity = []
    for question, nearest, _ in answerable:
        gold = [c.similarity for c in nearest if passage_of[c.chunk_id] == question["answer"]]
        gold_similarity.append(gold[0] if gold else float("nan"))
    best_unanswerable = {
        kind: [
            max((c.similarity for c in n), default=0.0)
            for q, n, _ in unanswerable
            if q["kind"] == kind
        ]
        for kind in ("near_miss", "off_topic")
    }

    sweep = []
    for floor in floors:
        recall = sum(
            q["answer"] in chosen_ids(q["question"], n, m, floor, keywords=True)
            for q, n, m in answerable
        ) / len(answerable)
        leak = sum(
            bool(chosen_ids(q["question"], n, m, floor, keywords=True)) for q, n, m in unanswerable
        )
        leak_vector_only = sum(
            bool(chosen_ids(q["question"], n, m, floor, keywords=False)) for q, n, m in unanswerable
        )
        sweep.append(
            {
                "floor": floor,
                "recall": round(recall, 3),
                "leak": round(leak / len(unanswerable), 3),
                "leak_without_keywords": round(leak_vector_only / len(unanswerable), 3),
            }
        )
    leaks_by_question = [
        {
            "question": q["question"],
            "kind": q["kind"],
            "keyword_hits": [passage_of[c.chunk_id] for c in m],
        }
        for q, n, m in unanswerable
        if m
    ]
    return {
        "dataset_version": dataset["version"],
        "model": settings.embedding_model,
        "gold_similarity": {
            "min": round(min(gold_similarity), 3),
            "median": round(statistics.median(gold_similarity), 3),
        },
        "best_unanswerable_similarity": {
            kind: {"max": round(max(values), 3), "median": round(statistics.median(values), 3)}
            for kind, values in best_unanswerable.items()
        },
        "sweep": sweep,
        "keyword_hits_on_unanswerable": leaks_by_question,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args(argv)
    report = asyncio.run(evaluate(args.database_url, FLOORS))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
