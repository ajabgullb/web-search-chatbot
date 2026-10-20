/**
 * Turns `[S1]` (web) and `[D1]` (document) citation markers in rendered
 * Markdown into citation elements.
 *
 * Runs on the HTML tree *after* sanitizing, so the elements it creates are our
 * own and are never subject to — or a way around — the sanitizer. Text inside
 * code, preformatted blocks, and links is left alone: `[S1]` in a code sample
 * is code, not a citation.
 *
 * Each marker becomes `<sup data-citation="S1">[S1]</sup>`. The Markdown
 * component renders that as a button when the source is known, and as the
 * original text when it is not — content is never silently dropped.
 */

import type { Element, ElementContent, Root, RootContent } from 'hast'

const MARKER = /\[([SD]\d{1,3})\]/g
const OPAQUE_TAGS = new Set(['code', 'pre', 'a'])

function splitText(value: string): ElementContent[] {
  const parts: ElementContent[] = []
  let last = 0
  for (const match of value.matchAll(MARKER)) {
    const index = match.index
    if (index > last) parts.push({ type: 'text', value: value.slice(last, index) })
    parts.push({
      type: 'element',
      tagName: 'sup',
      properties: { dataCitation: match[1] },
      children: [{ type: 'text', value: match[0] }],
    })
    last = index + match[0].length
  }
  if (parts.length === 0) return [{ type: 'text', value }]
  if (last < value.length) parts.push({ type: 'text', value: value.slice(last) })
  return parts
}

function transform(node: Root | Element): void {
  const children: (RootContent | ElementContent)[] = []
  for (const child of node.children) {
    if (child.type === 'text') {
      children.push(...splitText(child.value))
    } else {
      if (child.type === 'element' && !OPAQUE_TAGS.has(child.tagName)) transform(child)
      children.push(child)
    }
  }
  node.children = children
}

export default function rehypeCitations() {
  return (tree: Root) => {
    transform(tree)
  }
}
