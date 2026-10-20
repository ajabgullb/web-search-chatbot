import type { Root } from 'hast'
import { describe, expect, it } from 'vitest'

import rehypeCitations from '@/features/chat/rehypeCitations'

function paragraph(...children: Root['children']): Root {
  return {
    type: 'root',
    children: [{ type: 'element', tagName: 'p', properties: {}, children }] as Root['children'],
  }
}

describe('rehypeCitations', () => {
  it('turns markers into citation elements and keeps the surrounding text', () => {
    const tree = paragraph({ type: 'text', value: 'Rates rose [S1][S2] today.' })
    rehypeCitations()(tree)

    expect(JSON.stringify(tree)).toBe(
      JSON.stringify(
        paragraph(
          { type: 'text', value: 'Rates rose ' },
          {
            type: 'element',
            tagName: 'sup',
            properties: { dataCitation: 'S1' },
            children: [{ type: 'text', value: '[S1]' }],
          },
          {
            type: 'element',
            tagName: 'sup',
            properties: { dataCitation: 'S2' },
            children: [{ type: 'text', value: '[S2]' }],
          },
          { type: 'text', value: ' today.' },
        ),
      ),
    )
  })

  it('recognises document markers as well as web ones', () => {
    const tree = paragraph({ type: 'text', value: 'Grew [D1] and fell [S2]; [X3] is text.' })
    rehypeCitations()(tree)

    const labels = JSON.stringify(tree).match(/"dataCitation":"([A-Z]\d+)"/g)
    expect(labels).toEqual(['"dataCitation":"D1"', '"dataCitation":"S2"'])
  })

  it('leaves code, preformatted text, and link text alone', () => {
    for (const tagName of ['code', 'pre', 'a']) {
      const tree = paragraph({
        type: 'element',
        tagName,
        properties: {},
        children: [{ type: 'text', value: 'see [S1]' }],
      })
      const before = JSON.stringify(tree)
      rehypeCitations()(tree)
      expect(JSON.stringify(tree)).toBe(before)
    }
  })

  it('ignores text that only looks similar', () => {
    const tree = paragraph({ type: 'text', value: '[1] [S] [S1234] [note]' })
    const before = JSON.stringify(tree)
    rehypeCitations()(tree)
    expect(JSON.stringify(tree)).toBe(before)
  })
})
