"""Citation validation.

The model is asked to cite with the labels we supplied. It is not trusted to
have done so: a label it invents would render as a working citation and point
at nothing, which is worse than an uncited sentence because it looks verified.

So every marker in a generated answer is checked against the sources actually
retrieved. Unknown labels are removed from the text and never become citations.
"""

from __future__ import annotations

import logging
import re

from app.agent.state import Citation, Source

logger = logging.getLogger(__name__)

# Accepts [S1] and the [S1, S2] form models drift into, for web sources (S)
# and document chunks (D), mixed as in [S1, D2]. Bounded digits so a
# pathological string cannot produce a giant match.
_MARKER = re.compile(r"\[\s*([SD]\d{1,3}(?:\s*,\s*[SD]\d{1,3})*)\s*\]", re.IGNORECASE)

# Below this an answer is a greeting, a refusal, or a clarifying question —
# not a body of factual claims that citations were supposed to support.
_SUBSTANTIVE_ANSWER_CHARS = 160


def _make_citation(source: Source, ordinal: int) -> Citation:
    return Citation(
        ordinal=ordinal,
        source_id=source["source_id"],
        url=source["url"],
        display_url=source["display_url"],
        title=source["title"],
        publisher=source["publisher"],
        snippet=source["snippet"],
        published_at=source["published_at"],
        retrieved_at=source["retrieved_at"],
        kind=source.get("kind", "web"),
        document_id=source.get("document_id"),
        chunk_id=source.get("chunk_id"),
        page=source.get("page"),
    )


def _index_sources(sources: list[Source]) -> dict[str, Source]:
    return {source["source_id"].upper(): source for source in sources}


def _resolve_marker(
    labels_text: str,
    by_id: dict[str, Source],
    used: list[str],
    discarded: list[str],
) -> str:
    """Rewrite one marker, keeping only labels that name a real source.

    Shared by the batch validator and the streaming filter, so the text a user
    watches arrive and the text that is saved apply the same rule.
    """
    labels = [label.strip().upper() for label in labels_text.split(",")]
    kept = []
    for label in labels:
        if label in by_id:
            kept.append(label)
            if label not in used:
                used.append(label)
        elif label not in discarded:
            discarded.append(label)
    # Rewritten as separate brackets: one marker per source keeps the
    # frontend's anchoring rule trivial.
    return "".join(f"[{label}]" for label in kept)


def _tidy(text: str) -> str:
    """Collapse whitespace left behind where a discarded marker used to be."""
    return re.sub(r" +([.,;:])", r"\1", text).replace("  ", " ").strip()


def extract_citations(answer: str, sources: list[Source]) -> tuple[str, list[Citation], list[str]]:
    """Validate the markers in ``answer``.

    Returns the cleaned answer, the citations in first-use order, and the list
    of labels that were discarded. Valid markers are left in the text exactly as
    written so the frontend can anchor an inline citation to them.
    """
    by_id = _index_sources(sources)
    used: list[str] = []
    discarded: list[str] = []

    cleaned = _MARKER.sub(
        lambda match: _resolve_marker(match.group(1), by_id, used, discarded), answer
    )
    if discarded:
        logger.warning("citation_discarded", extra={"count": len(discarded)})

    citations = [_make_citation(by_id[label], index) for index, label in enumerate(used, start=1)]
    return _tidy(cleaned), citations, discarded


def strip_citation_markers(text: str) -> str:
    """Remove every citation marker from a stored answer.

    Used on earlier assistant turns before they go back to the model. Labels are
    assigned per run, so an old ``[S1]`` names a different page than this run's
    ``S1``; left in place, it invites the model to re-cite an old claim against
    a new, unrelated source.
    """
    return _tidy(_MARKER.sub("", text))


# A string that could still grow into a marker once more text arrives: "[",
# "[S", "[D1", "[S1, ", "[S1, D2". Anything else starting with "[" is plain text.
_PARTIAL_MARKER = re.compile(
    r"\[\s*(?:[SD]\d{0,3}(?:\s*,\s*(?:[SD]\d{0,3})?)*\s*)?\Z", re.IGNORECASE
)
# The longest plausible partial marker. Past this, "[" is text, not a citation
# in progress, and holding it back would visibly stall the stream.
_MAX_PENDING_CHARS = 48


class CitationStreamFilter:
    """Validate citation markers in a token stream, one chunk at a time.

    A model's output arrives in arbitrary fragments, so "[S1]" may be split as
    "[", "S", "1]". The filter holds back only a trailing fragment that could
    still become a marker, and releases everything else immediately. Valid
    markers pass through and report their citation the first time each source is
    used; invalid ones are dropped before the user ever sees them.

    Feeding it a whole answer yields the same citations, in the same order, as
    :func:`extract_citations` — the saved message and the stream agree on which
    sources were cited. Only whitespace tidying differs, which is why clients
    replace streamed text with the canonical message on completion.
    """

    def __init__(self, sources: list[Source]) -> None:
        self._by_id = _index_sources(sources)
        self._used: list[str] = []
        self._discarded: list[str] = []
        self._pending = ""

    def feed(self, chunk: str) -> tuple[str, list[Citation]]:
        self._pending += chunk
        return self._drain(final=False)

    def flush(self) -> tuple[str, list[Citation]]:
        """Release whatever is held back; the stream has ended."""
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> tuple[str, list[Citation]]:
        text = self._pending
        first_new = len(self._used)
        out: list[str] = []
        position = 0
        held_from: int | None = None

        while position < len(text):
            start = text.find("[", position)
            if start == -1:
                out.append(text[position:])
                position = len(text)
                break
            out.append(text[position:start])

            match = _MARKER.match(text, start)
            if match is not None:
                out.append(
                    _resolve_marker(match.group(1), self._by_id, self._used, self._discarded)
                )
                position = match.end()
                continue

            tail = text[start:]
            if not final and len(tail) <= _MAX_PENDING_CHARS and _PARTIAL_MARKER.match(tail):
                held_from = start
                break
            # Not a marker and cannot become one: an ordinary bracket.
            out.append("[")
            position = start + 1

        self._pending = text[held_from:] if held_from is not None else ""
        new_citations = [
            _make_citation(self._by_id[label], index)
            for index, label in enumerate(self._used, start=1)
            if index > first_new
        ]
        return "".join(out), new_citations


def is_degraded(*, searched: bool, answer: str, citations: list[Citation]) -> bool:
    """Should this answer be flagged as unverified?

    A run that searched and still produced a substantial answer with no valid
    citation has failed at the one thing searching was for. We do not try to
    detect "factual claims" in the prose — no reliable test exists — so the rule
    errs toward flagging: the cost of a needless flag is a caveat the user
    ignores, the cost of a missed one is an answer that looks sourced.
    """
    if not searched:
        return False
    return not citations and len(answer.strip()) >= _SUBSTANTIVE_ANSWER_CHARS
