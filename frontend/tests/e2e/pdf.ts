/**
 * Build a small, valid PDF in memory: one Helvetica text object per page.
 *
 * The same construction as backend/tests/pdfs.py, so a spec shows the exact
 * text its document holds instead of shipping a binary fixture. The server
 * parses it for real (pypdf, in a child process); only embeddings are faked.
 */

function escape(text: string): string {
  return text.replace(/\\/g, '\\\\').replace(/\(/g, '\\(').replace(/\)/g, '\\)')
}

export function makePdf(pages: string[]): Buffer {
  const objects = new Map<number, string>([
    [1, '<< /Type /Catalog /Pages 2 0 R >>'],
    [3, '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'],
  ])
  const kids: number[] = []
  let nextId = 4
  for (const text of pages) {
    const pageId = nextId
    const contentId = nextId + 1
    nextId += 2
    const lines = text ? text.split('\n').map((line) => `(${escape(line)}) Tj T*`) : []
    const stream = ['BT', '/F1 11 Tf', '14 TL', '50 750 Td', ...lines, 'ET'].join('\n')
    objects.set(
      contentId,
      `<< /Length ${Buffer.byteLength(stream, 'latin1')} >>\nstream\n${stream}\nendstream`,
    )
    objects.set(
      pageId,
      '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] ' +
        `/Resources << /Font << /F1 3 0 R >> >> /Contents ${contentId} 0 R >>`,
    )
    kids.push(pageId)
  }
  objects.set(
    2,
    `<< /Type /Pages /Kids [${kids.map((kid) => `${kid} 0 R`).join(' ')}] /Count ${kids.length} >>`,
  )

  let out = '%PDF-1.4\n'
  const offsets = new Map<number, number>()
  for (const number of [...objects.keys()].sort((a, b) => a - b)) {
    offsets.set(number, Buffer.byteLength(out, 'latin1'))
    out += `${number} 0 obj\n${objects.get(number)}\nendobj\n`
  }
  const xrefAt = Buffer.byteLength(out, 'latin1')
  const size = Math.max(...objects.keys()) + 1
  out += `xref\n0 ${size}\n0000000000 65535 f \n`
  for (let number = 1; number < size; number++) {
    out += `${String(offsets.get(number)).padStart(10, '0')} 00000 n \n`
  }
  out += `trailer\n<< /Size ${size} /Root 1 0 R >>\nstartxref\n${xrefAt}\n%%EOF\n`
  return Buffer.from(out, 'latin1')
}
