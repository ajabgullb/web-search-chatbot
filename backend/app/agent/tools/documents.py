"""Choosing which chunks of the user's documents an answer gets to read.

Pure functions plus the small types they work on. The database and the
embedding call live in ``app/services/retrieval.py``; everything that decides
*which* chunks win lives here, where it can be tested without either.

The rules (ADR-0015):

- **Two rankings, fused.** Embedding similarity finds chunks that mean the
  same thing; full-text search finds the exact names, codes, and numbers that
  embeddings blur. Reciprocal rank fusion merges them by rank alone, because a
  cosine similarity and a text-search rank are on scales that cannot be
  compared directly.
- **A relevance floor.** A chunk is used only if its similarity reaches the
  floor. Below that it is noise, and an answer written from noise is how
  invented citations happen. The one exception is an exact match on a term
  containing a digit (a part number, an invoice number, a year): embeddings
  blur those, and they are what the keyword half is for. Ordinary shared
  words only help a chunk's rank. Measured with ``evals/retrieval.py``:
  letting any keyword match past the floor raised the share of unanswerable
  questions that still got a chunk from 30% to 40% and recovered nothing.
- **A budget.** At most ``document_top_k`` chunks and
  ``document_context_chars`` characters reach the prompt.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.agent.state import Source

# The constant in reciprocal rank fusion. 60 is the value from the original
# paper (Cormack et al., 2009) and the usual default: large enough that the
# top few ranks do not drown out agreement between the two lists.
RRF_K = 60

MAX_KEYWORDS = 12

# Common English words that would match almost every chunk. Removing them
# keeps the keyword half for the words that actually distinguish one passage
# from another. Questions in other languages keep all their words; the
# embedding half still carries them.
STOPWORDS = frozenset(
    [
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
        "tell",
        "show",
        "give",
        "find",
        "explain",
        "describe",
        "say",
        "says",
        "said",
        "please",
        "document",
        "documents",
        "pdf",
        "file",
        "files",
        "attached",
        "uploaded",
        "report",
        "paper",
    ]
)

# A word: letters and digits. Keeping only these means a keyword can never
# carry text-search operators (&, |, !, :, parentheses) into the query.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ChunkCandidate:
    """One chunk as a search method returned it."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    page: int
    content: str
    # Cosine similarity to the question, from -1 to 1. Present for every
    # candidate, whichever method found it, so the floor can always apply.
    similarity: float


@dataclass(frozen=True, slots=True)
class RankedChunk:
    candidate: ChunkCandidate
    score: float
    keyword_match: bool


@dataclass(slots=True)
class DocumentSearch:
    """What retrieval produced for one question."""

    chunks: list[RankedChunk] = field(default_factory=list)
    # The question could not be embedded (provider down, timeout). Distinct
    # from "searched and found nothing relevant".
    failed: bool = False


def keyword_terms(question: str) -> list[str]:
    """The distinguishing words of a question, lowercased, in order, deduplicated."""
    terms: list[str] = []
    for word in _WORD.findall(question.lower()):
        if word in STOPWORDS or (len(word) < 2 and not word.isdigit()):
            continue
        if word not in terms:
            terms.append(word)
        if len(terms) == MAX_KEYWORDS:
            break
    return terms


def exact_terms(terms: Sequence[str]) -> list[str]:
    """Terms precise enough to bypass the floor on an exact match: those with a digit."""
    return [term for term in terms if any(ch.isdigit() for ch in term)]


def keyword_query(terms: Sequence[str]) -> str:
    """A ``to_tsquery`` expression matching any of the terms.

    OR, not AND: a natural question rarely has every one of its words in a
    single passage, and ``websearch_to_tsquery`` would demand exactly that.
    Ranking then prefers chunks that match more of them.
    """
    return " | ".join(terms)


def fuse(
    by_similarity: Sequence[ChunkCandidate],
    by_keywords: Sequence[ChunkCandidate],
    *,
    k: int = RRF_K,
) -> list[RankedChunk]:
    """Merge the two rankings: each list adds ``1 / (k + rank)`` to a chunk's score."""
    scores: dict[uuid.UUID, float] = {}
    candidates: dict[uuid.UUID, ChunkCandidate] = {}
    for rank, candidate in enumerate(by_similarity, start=1):
        scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + 1.0 / (k + rank)
        candidates[candidate.chunk_id] = candidate
    keyword_ids = set()
    for rank, candidate in enumerate(by_keywords, start=1):
        scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + 1.0 / (k + rank)
        candidates.setdefault(candidate.chunk_id, candidate)
        keyword_ids.add(candidate.chunk_id)
    ranked = [
        RankedChunk(candidates[chunk_id], score, chunk_id in keyword_ids)
        for chunk_id, score in scores.items()
    ]
    # Ties broken by similarity, then id, so the order never depends on
    # dictionary iteration and a test can pin it.
    ranked.sort(key=lambda r: (-r.score, -r.candidate.similarity, str(r.candidate.chunk_id)))
    return ranked


def _contains_any(content: str, terms: set[str]) -> bool:
    return bool(terms) and not terms.isdisjoint(_WORD.findall(content.lower()))


def select(
    ranked: Sequence[RankedChunk],
    *,
    min_similarity: float,
    top_k: int,
    max_chars: int,
    exact: Sequence[str] = (),
) -> list[RankedChunk]:
    """The chunks worth reading, best first, within the budget.

    ``exact`` is ``exact_terms`` of the question: a chunk containing one of
    them is kept below the floor.
    """
    exact_set = set(exact)
    chosen: list[RankedChunk] = []
    used = 0
    for chunk in ranked:
        if len(chosen) == top_k:
            break
        relevant = chunk.candidate.similarity >= min_similarity or (
            chunk.keyword_match and _contains_any(chunk.candidate.content, exact_set)
        )
        if not relevant:
            continue
        size = len(chunk.candidate.content)
        # The best chunk always fits; later ones only within the budget.
        if chosen and used + size > max_chars:
            continue
        chosen.append(chunk)
        used += size
    return chosen


def to_sources(chunks: Sequence[RankedChunk], *, retrieved_at: datetime) -> list[Source]:
    """Citable sources labelled ``D1``, ``D2``… in relevance order."""
    return [
        Source(
            source_id=f"D{index}",
            url=None,
            display_url=None,
            title=chunk.candidate.filename,
            snippet=chunk.candidate.content,
            publisher=None,
            published_at=None,
            retrieved_at=retrieved_at,
            kind="document",
            document_id=str(chunk.candidate.document_id),
            chunk_id=str(chunk.candidate.chunk_id),
            page=chunk.candidate.page,
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def retrieval_query(question: str, earlier_questions: Sequence[str]) -> str:
    """The text to search the documents with, when no rewrite is available.

    A short follow-up ("and the second one?") means nothing on its own, so the
    previous question is prepended. A cheap stand-in for a model rewrite,
    which is used instead whenever the routing step asked the model anyway.
    """
    words = question.split()
    if len(words) <= 6 and earlier_questions:
        return f"{earlier_questions[-1]} {question}"
    return question
