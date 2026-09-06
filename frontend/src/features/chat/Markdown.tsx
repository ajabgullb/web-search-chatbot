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
 * There is no `dangerouslySetInnerHTML` anywhere in this file, by design.
 */

import ReactMarkdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'

/** Only ever link somewhere a browser can safely navigate. */
function isSafeHref(href: string | undefined): href is string {
  if (!href) return false
  try {
    const url = new URL(href, window.location.origin)
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

export default function Markdown({ content }: { content: string }) {
  return (
    <div className="prose-chat">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        // Sanitize first, then highlight: highlighting only adds <span> and
        // hljs-* classes, and the default schema keeps the `language-*` class
        // on <code> that the highlighter reads.
        rehypePlugins={[rehypeSanitize, rehypeHighlight]}
        components={{
          a: ({ href, children, ...props }) =>
            isSafeHref(href) ? (
              <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
                {children}
              </a>
            ) : (
              // Keep the text, drop the link. Silently rendering nothing would
              // hide part of the answer.
              <span>{children}</span>
            ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
