/**
 * Renders model output as Markdown, safely.
 *
 * Three layers, because model output is untrusted text and one day some of it
 * will be quoted straight from a web page the agent searched:
 *
 * 1. `react-markdown` does not render raw HTML unless `rehype-raw` is added.
 *    It is not added, so `<img onerror=...>` in the model's answer is text.
 * 2. `rehype-sanitize` runs anyway, on the default hast schema. Belt and
 *    braces: it also survives someone adding a plugin later that reintroduces
 *    HTML nodes.
 * 3. Links are re-checked here for scheme and given `rel="noopener noreferrer"`.
 *    Sanitize already drops `javascript:`, but this is the rule AGENTS.md §6
 *    states outright, so it is enforced where it can be read.
 *
 * Citation markers (`[S1]`) become buttons that jump to the matching source
 * card. They are created after sanitizing, by our own plugin, from text the
 * sanitizer has already accepted.
 *
 * There is no `dangerouslySetInnerHTML` anywhere in this file, by design.
 */

import ReactMarkdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'

import type { Citation } from '@/api/types'
import rehypeCitations from '@/features/chat/rehypeCitations'
import { sourceAnchorId } from '@/features/chat/sourceAnchors'
import { isSafeHref } from '@/lib/safeUrl'

interface MarkdownProps {
  content: string
  citations?: Citation[]
  /** Prefix of the source cards' element ids, unique per message. */
  anchorPrefix?: string
}

function focusSource(anchorPrefix: string, sourceId: string): void {
  const card = document.getElementById(sourceAnchorId(anchorPrefix, sourceId))
  if (!card) return
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  card.scrollIntoView?.({ block: 'nearest', behavior: reduceMotion ? 'auto' : 'smooth' })
  // Focus moves with the jump, so a keyboard or screen-reader user lands on
  // the source rather than having to find it.
  card.focus({ preventScroll: true })
}

export default function Markdown({ content, citations = [], anchorPrefix = 'msg' }: MarkdownProps) {
  const bySource = new Map(citations.map((citation) => [citation.source_id, citation]))

  return (
    <div className="prose-chat">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        // Sanitize first, then highlight: highlighting only adds <span> and
        // hljs-* classes, and the default schema keeps the `language-*` class
        // on <code> that the highlighter reads. Citations last, on the
        // already-sanitized tree.
        rehypePlugins={[rehypeSanitize, rehypeHighlight, rehypeCitations]}
        components={{
          a: ({ href, children, node: _node, ...props }) =>
            isSafeHref(href) ? (
              <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
                {children}
              </a>
            ) : (
              // Keep the text, drop the link. Silently rendering nothing would
              // hide part of the answer.
              <span>{children}</span>
            ),
          sup: ({ node, children, ...props }) => {
            const sourceId = node?.properties.dataCitation
            if (typeof sourceId !== 'string') return <sup {...props}>{children}</sup>
            const citation = bySource.get(sourceId)
            // An unknown label shows as the plain text it was, never as a link
            // to nothing. The server strips these; this is the second lock.
            if (!citation) return <>{children}</>
            return (
              <sup className="citation-marker">
                <button
                  type="button"
                  className="citation-button"
                  title={citation.title}
                  aria-label={`Source ${citation.ordinal}: ${citation.title}`}
                  onClick={() => focusSource(anchorPrefix, citation.source_id)}
                >
                  {citation.ordinal}
                </button>
              </sup>
            )
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
