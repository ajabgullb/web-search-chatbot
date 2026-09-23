/**
 * The numbered sources under an answer.
 *
 * Each card is the target an inline citation button jumps to, so it can take
 * focus (`tabIndex={-1}`: reachable by script, not added to the tab order).
 * Links open in a new tab with `noopener noreferrer`, and only after the URL
 * is re-checked here — the server validated it, and this is the second lock.
 */

import { ExternalLink } from 'lucide-react'

import type { Citation } from '@/api/types'
import { sourceAnchorId } from '@/features/chat/sourceAnchors'
import { isSafeExternalUrl } from '@/lib/safeUrl'

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' })

function published(value: string | null | undefined): string | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : dateFormat.format(date)
}

export function SourceList({
  citations,
  anchorPrefix,
}: {
  citations: Citation[]
  anchorPrefix: string
}) {
  if (citations.length === 0) return null

  return (
    <section aria-label="Sources" className="mt-3 border-t border-border pt-3">
      <h3 className="mb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        Sources
      </h3>
      <ol className="flex flex-col gap-2">
        {citations.map((citation) => {
          const date = published(citation.published_at)
          const meta = [citation.display_url, citation.publisher, date].filter(Boolean).join(' · ')
          return (
            <li
              key={citation.source_id}
              id={sourceAnchorId(anchorPrefix, citation.source_id)}
              tabIndex={-1}
              className="flex gap-2 rounded-md p-1.5 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring focus:bg-accent"
            >
              <span
                aria-hidden
                className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded bg-muted font-medium tabular-nums"
              >
                {citation.ordinal}
              </span>
              <div className="min-w-0">
                {isSafeExternalUrl(citation.url) ? (
                  <a
                    href={citation.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 font-medium text-primary underline-offset-2 hover:underline"
                  >
                    <span className="sr-only">Source {citation.ordinal}: </span>
                    {citation.title}
                    <ExternalLink className="size-3 shrink-0" aria-hidden />
                    <span className="sr-only"> (opens in a new tab)</span>
                  </a>
                ) : (
                  <span className="font-medium">{citation.title}</span>
                )}
                <p className="truncate text-muted-foreground">{meta}</p>
                {citation.snippet ? (
                  <p className="mt-0.5 line-clamp-2 text-muted-foreground">{citation.snippet}</p>
                ) : null}
              </div>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
