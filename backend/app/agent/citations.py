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

# Accepts [S1] and the [S1, S2] form models drift into. Bounded digits so a
# pathological string cannot produce a giant match.
_MARKER = re.compile(r"\[\s*(S\d{1,3}(?:\s*,\s*S\d{1,3})*)\s*\]", re.IGNORECASE)

# Below this an answer is a greeting, a refusal, or a clarifying question —
# not a body of factual claims that citations were supposed to support.
_SUBSTANTIVE_ANSWER_CHARS = 160


def extract_citations(answer: str, sources: list[Source]) -> tuple[str, list[Citation], list[str]]:
    """Validate the markers in ``answer``.

    Returns the cleaned answer, the citations in first-use order, and the list
    of labels that were discarded. Valid markers are left in the text exactly as
    written so the frontend can anchor an inline citation to them.
    """
    by_id = {source["source_id"].upper(): source for source in sources}
    used: list[str] = []
    discarded: list[str] = []

    def replace(match: re.Match[str]) -> str:
        labels = [label.strip().upper() for label in match.group(1).split(",")]
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

    cleaned = _MARKER.sub(replace, answer)
    if discarded:
        logger.warning("citation_discarded count=%d labels=%s", len(discarded), discarded)

    citations = [
        Citation(
            ordinal=index,
            source_id=by_id[label]["source_id"],
            url=by_id[label]["url"],
            title=by_id[label]["title"],
            publisher=by_id[label]["publisher"],
            snippet=by_id[label]["snippet"],
            published_at=by_id[label]["published_at"],
            retrieved_at=by_id[label]["retrieved_at"],
        )
        for index, label in enumerate(used, start=1)
    ]
    # Collapse whitespace left behind where a discarded marker used to be.
    return re.sub(r" +([.,;:])", r"\1", cleaned).replace("  ", " ").strip(), citations, discarded


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
