"""Build small, valid PDFs in memory for tests.

Generated rather than committed as binaries, so a test shows exactly what text
its document holds. The output is a plain PDF 1.4 file with one Helvetica text
object per page: enough for ``pypdf`` to extract, nothing more.
"""

from __future__ import annotations

import io


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[str]) -> bytes:
    """One page per string; ``\\n`` starts a new line. An empty string is a blank page."""
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids: list[int] = []
    next_id = 4
    for text in pages:
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        operators = ["BT", "/F1 11 Tf", "14 TL", "50 750 Td"]
        operators += [f"({_escape(line)}) Tj T*" for line in text.split("\n") if text]
        operators.append("ET")
        stream = "\n".join(operators).encode("latin-1")
        objects[content_id] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        objects[page_id] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_id
        )
        kids.append(page_id)
    references = " ".join(f"{kid} 0 R" for kid in kids)
    objects[2] = f"<< /Type /Pages /Kids [{references}] /Count {len(kids)} >>".encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])
    xref_at = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref_at)
    return bytes(out)


def encrypted_pdf(pages: list[str], *, user_password: str, owner_password: str = "owner") -> bytes:
    """``user_password=""`` gives the common case: readable, but restricted."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf(pages))))
    # RC4 needs no optional dependency; AES would need ``cryptography``.
    writer.encrypt(user_password, owner_password, algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
