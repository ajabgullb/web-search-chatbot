"""Deciding whether to search, and where the graph goes next.

Heuristics run first and the model is consulted only when they are silent. That
ordering is deliberate: a greeting should not cost a model call to classify, the
obvious cases are the common ones, and a deterministic rule is testable in a way
that a model's judgement is not. The model handles the genuinely ambiguous
middle, which is where it earns its latency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

MAX_MODEL_QUERIES = 3


class SearchDecision(BaseModel):
    """The structured reply the model must produce when heuristics abstain."""

    should_search: bool = Field(description="True when a web search is needed.")
    rationale: str = Field(default="", max_length=200, description="One short clause.")
    queries: list[str] = Field(
        default_factory=list,
        max_length=MAX_MODEL_QUERIES,
        description="Keyword queries to run; empty when not searching.",
    )
    document_query: str = Field(
        default="",
        max_length=400,
        description=(
            "When documents are attached: the question rewritten to stand on its own, "
            "for searching them. Empty otherwise."
        ),
    )


@dataclass(frozen=True, slots=True)
class HeuristicDecision:
    """``should_search`` is ``None`` when the heuristics decline to decide."""

    should_search: bool | None
    rationale: str


def _pattern(*alternatives: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b", re.IGNORECASE)


# Nothing here needs the web. Anchored to the whole message so that "hi, what is
# the current bitcoin price" is not mistaken for a greeting.
_GREETING = re.compile(
    r"^\W*(?:hi|hey|hello|yo|thanks|thank you|thx|ty|good (?:morning|afternoon|evening)"
    r"|how are you|what'?s up|bye|goodbye|see you)\W*$",
    re.IGNORECASE,
)

# Work performed on text the user supplied. The material is already in the
# conversation, so searching for it would be searching for nothing.
#
# A verb alone proves nothing ("fix my bike chain"), so the verb must point
# at text: a word like "this" right after it, or a noun that names text.
_TEXT_TASK_VERB = _pattern(
    "rewrite",
    "reword",
    "rephrase",
    "paraphrase",
    "translate",
    "proofread",
    "summarise",
    "summarize",
    "shorten",
    "expand",
    "format",
    "reformat",
    "edit",
    "correct",
    "fix",
)
# "Summarize this", "translate the following". Not "summarize this week's
# news": there "this" belongs to a time phrase, not to a piece of text.
_TEXT_TASK_POINTER = re.compile(
    _TEXT_TASK_VERB.pattern + r"\s+(?:(?:this|that|it|these|the\s+following|below|above)\b"
    r"(?!\s+(?:week|weekend|month|year|morning|afternoon|evening|quarter|season)\b))",
    re.IGNORECASE,
)
_TEXT_NOUN = _pattern(
    "text",
    "paragraphs?",
    "sentences?",
    "passage",
    "emails?",
    "e-mails?",
    "messages?",
    "letter",
    "essay",
    "draft",
    "grammar",
    "spelling",
    "wording",
    "phrasing",
    "tone",
    "bio",
    "caption",
    "tweet",
    "code",
    "snippet",
)
# A how-to question is asking for knowledge, not handing over text to work on.
_HOW_TO = re.compile(r"^\W*(?:how|why|what|when|where|which|who)\b", re.IGNORECASE)

# The material is in the message itself: after a colon, inside quotes or a
# code fence, or on a line of its own.
_SUPPLIED_TEXT = re.compile(r':\s+\S|```|"[^"\n]{3,}"|\n\s*\S')

# Answers that change over time. These must be looked up, because a model's
# recollection of them is stale by construction.
_FRESHNESS = _pattern(
    "today",
    "tonight",
    "yesterday",
    "tomorrow",
    "latest",
    "newest",
    "recent",
    "recently",
    "news",
    "breaking",
    "right now",
    "so far",
    "this week",
    "this month",
    "this year",
    "as of",
    "up to date",
    "upcoming",
    "release date",
    "weather",
    "forecast",
    "who won",
    "nowadays",
)
# Words that often mean "changes over time" but also have a stable meaning:
# the current in a circuit, a soup stock, a z-score, when Python 3 was
# released. One of them alone is left to the model; two together ("the
# current price") are as clear as any word above.
_WEAK_FRESHNESS = _pattern(
    "current",
    "currently",
    "still",
    "price",
    "prices",
    "stock",
    "stocks",
    "score",
    "scores",
    "released",
)
_YEAR = re.compile(r"\b20(?:2[4-9]|[3-9]\d)\b")

# An explicit request for evidence. Phrase-level rather than word-level so that
# "open source" and "a link list" do not trigger a search on their own.
_WANTS_SOURCES = re.compile(
    r"(?:cite\s+(?:your|the|some|any)|citations?\b|with\s+sources|provide\s+sources"
    r"|give\s+me\s+sources|any\s+sources|list\s+sources|link\s+to|references?\s+for"
    r"|back\s+(?:this|that|it)\s+up|according\s+to\s+(?:the\s+)?(?:latest|current))",
    re.IGNORECASE,
)


# The question is about the attached files. Only consulted when the
# conversation has documents, so "the report" in a chat without one means
# nothing here.
_REFERS_TO_DOCUMENTS = _pattern(
    "document",
    "documents",
    "doc",
    "docs",
    "pdf",
    "pdfs",
    "file",
    "files",
    "attachment",
    "attachments",
    "attached",
    "uploaded",
    "upload",
    "report",
    "paper",
    "manual",
    "contract",
    "slides",
)
# An explicit ask for the web, which wins over "it is about the document".
_WANTS_WEB = re.compile(
    r"\b(?:search\s+(?:the\s+)?(?:web|internet|online)|on\s+the\s+(?:web|internet)|online|google)\b",
    re.IGNORECASE,
)


def supplies_text(question: str) -> bool:
    """The message carries the material it asks to have worked on."""
    return bool(_SUPPLIED_TEXT.search(question.strip()))


def _is_fresh(text: str) -> bool:
    if _FRESHNESS.search(text) or _YEAR.search(text):
        return True
    return len(_WEAK_FRESHNESS.findall(text)) >= 2


def is_text_task(question: str) -> bool:
    """A request to rewrite, translate, summarize... some piece of text.

    The text may be in the message, in an earlier message, or in an attached
    document; :func:`supplies_text` says whether it is in the message.
    """
    text = question.strip()
    if supplies_text(text):
        # "Rewrite this: the latest release..." — words inside the supplied
        # text say nothing about what the request needs.
        instruction = _SUPPLIED_TEXT.split(text, maxsplit=1)[0]
        return bool(_TEXT_TASK_VERB.search(instruction))
    if _HOW_TO.match(text) or _is_fresh(text):
        # No text in sight, and the question is about the world.
        return False
    if _TEXT_TASK_POINTER.search(text):
        return True
    return bool(_TEXT_TASK_VERB.search(text) and _TEXT_NOUN.search(text))


def is_conversational(question: str) -> str | None:
    """Why a message needs no web search, or None if it might."""
    text = question.strip()
    if not text:
        return "empty message"
    if _GREETING.match(text):
        return "greeting or small talk"
    if is_text_task(text):
        return "operates on text already supplied"
    return None


def refers_to_documents(question: str) -> bool:
    """The question is about the attached documents.

    Text supplied in the message is left out: "rewrite this: send me the
    report" mentions a report, but asks about the sentence.
    """
    text = question.strip()
    if supplies_text(text):
        text = _SUPPLIED_TEXT.split(text, maxsplit=1)[0]
    return bool(_REFERS_TO_DOCUMENTS.search(text))


def wants_web(question: str) -> bool:
    return bool(_WANTS_WEB.search(question))


def heuristic_search_decision(question: str) -> HeuristicDecision:
    """Classify a question without a model call where the answer is obvious."""
    text = question.strip()
    conversational = is_conversational(text)
    if conversational is not None:
        return HeuristicDecision(False, conversational)

    if _WANTS_SOURCES.search(text):
        return HeuristicDecision(True, "user asked for sources")

    if _is_fresh(text):
        return HeuristicDecision(True, "answer depends on current information")

    # Everything else is genuinely ambiguous; let the model look at it.
    return HeuristicDecision(None, "")


def route_after_decision(*, use_documents: bool, should_search: bool) -> str:
    if use_documents:
        return "retrieve_documents"
    return "web_search" if should_search else "answer_direct"


def route_after_documents(*, should_search: bool, source_count: int) -> str:
    """Web search next if planned; otherwise answer from what was found."""
    if should_search:
        return "web_search"
    return "synthesize" if source_count > 0 else "answer_direct"


def route_after_search(source_count: int) -> str:
    """Where to go once searching is done.

    With no usable source there is nothing to synthesize from. Routing to
    ``answer_direct`` answers from the model's own knowledge instead, under a
    prompt that forbids citations — which is honest. Sending an empty source
    list to ``synthesize`` would invite exactly the fabricated citation the
    whole pipeline exists to prevent.
    """
    return "synthesize" if source_count > 0 else "answer_direct"
