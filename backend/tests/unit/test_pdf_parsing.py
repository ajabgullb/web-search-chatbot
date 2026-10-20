"""PDF parsing in a child process, end to end with real processes.

The point of the child process is containment, so the failure paths matter as
much as the happy one: a parse that hangs is killed, one that crashes or runs
out of memory is reported, and none of it touches the test process.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.services.pdf import PdfError, PdfParser, PdfParserBusy
from app.services.pdf_worker import ParseLimits
from tests import pdf_targets
from tests.pdfs import encrypted_pdf, make_pdf

LIMITS = ParseLimits(max_pages=5, max_chars=2_000, memory_bytes=1024 * 1024 * 1024)


def parser(**overrides: object) -> PdfParser:
    values: dict[str, object] = {"limits": LIMITS, "timeout_seconds": 20.0, "max_concurrent": 2}
    values.update(overrides)
    return PdfParser(**values)  # type: ignore[arg-type]


async def failure_code(pdf_parser: PdfParser, data: bytes) -> str:
    with pytest.raises(PdfError) as caught:
        await pdf_parser.parse(data)
    return caught.value.code


async def test_each_page_is_extracted_in_order() -> None:
    pages = await parser().parse(make_pdf(["Alpha page", "", "Gamma page"]))
    assert [page.strip() for page in pages] == ["Alpha page", "", "Gamma page"]


async def test_a_password_protected_pdf_is_refused() -> None:
    assert await failure_code(parser(), encrypted_pdf(["secret"], user_password="pw")) == (
        "pdf_encrypted"
    )


async def test_a_pdf_encrypted_only_against_editing_is_read() -> None:
    """Common in the wild: an empty password to read, a real one to edit."""
    pages = await parser().parse(encrypted_pdf(["readable"], user_password=""))
    assert pages[0].strip() == "readable"


async def test_a_pdf_with_no_text_layer_is_refused() -> None:
    assert await failure_code(parser(), make_pdf(["", ""])) == "pdf_no_text"


async def test_bytes_that_only_start_like_a_pdf_are_refused() -> None:
    assert await failure_code(parser(), b"%PDF-1.4\nthis is not a pdf at all") == "pdf_invalid"


async def test_too_many_pages_stops_early() -> None:
    with pytest.raises(PdfError) as caught:
        await parser().parse(make_pdf(["p"] * (LIMITS.max_pages + 1)))
    assert (caught.value.code, caught.value.limit) == ("pdf_too_large", "pages")


async def test_too_much_text_stops_early() -> None:
    with pytest.raises(PdfError) as caught:
        await parser().parse(make_pdf(["word " * 300, "word " * 300]))
    assert (caught.value.code, caught.value.limit) == ("pdf_too_large", "characters")


async def test_a_hanging_parse_is_killed_at_the_deadline() -> None:
    started = time.monotonic()
    code = await failure_code(parser(timeout_seconds=0.5, target=pdf_targets.hang), make_pdf(["x"]))
    assert code == "pdf_too_complex"
    assert time.monotonic() - started < 10


async def test_a_crashing_parse_is_reported_not_propagated() -> None:
    assert (
        await failure_code(parser(target=pdf_targets.crash), make_pdf(["x"])) == "pdf_too_complex"
    )


async def test_a_parse_over_its_memory_allowance_fails_cleanly() -> None:
    """Far too little address space to parse anything: the child fails or dies,
    and either way the API process sees a clean error."""
    starved = ParseLimits(max_pages=5, max_chars=2_000, memory_bytes=32 * 1024 * 1024)
    assert await failure_code(parser(limits=starved), make_pdf(["x"])) == "pdf_too_complex"


async def test_a_full_parser_refuses_instead_of_queueing() -> None:
    busy = parser(max_concurrent=1, timeout_seconds=2.0, target=pdf_targets.hang)
    first = asyncio.create_task(busy.parse(make_pdf(["x"])))
    await asyncio.sleep(0.2)
    with pytest.raises(PdfParserBusy):
        await busy.parse(make_pdf(["y"]))
    with pytest.raises(PdfError):
        await first
