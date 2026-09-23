/**
 * AGENTS.md §9: model output is rendered as sanitized Markdown and never
 * injected as HTML. The simplest durable guarantee is that the escape hatch
 * does not appear in the application at all.
 */

import { describe, expect, it } from 'vitest'

const sources = import.meta.glob<string>(['/src/**/*.{ts,tsx}', '!/src/**/*.test.{ts,tsx}'], {
  query: '?raw',
  import: 'default',
  eager: true,
})

describe('raw HTML injection', () => {
  it('scans the application source', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(20)
  })

  it('sets dangerouslySetInnerHTML nowhere', () => {
    // The prop being *set* — as JSX (`=`) or as an object key (`:`) — not the
    // word, which the Markdown component's documentation mentions.
    const usage = /dangerouslySetInnerHTML\s*[=:]/
    const offenders = Object.entries(sources)
      .filter(([, text]) => usage.test(text))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })

  it('would catch a use if one appeared', () => {
    const usage = /dangerouslySetInnerHTML\s*[=:]/
    expect(usage.test('<div dangerouslySetInnerHTML={{ __html: x }} />')).toBe(true)
    expect(usage.test('createElement("div", { dangerouslySetInnerHTML: x })')).toBe(true)
  })
})
