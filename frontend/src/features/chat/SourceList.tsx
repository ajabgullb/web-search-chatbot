/**
 * The numbered sources under an answer.
 *
 * Each card is the target an inline citation button jumps to, so it can take
 * focus (`tabIndex={-1}`: reachable by script, not added to the tab order).
 * Links open in a new tab with `noopener noreferrer`, and only after the URL
 * is re-checked here — the server validated it, and this is the second lock.
 *
 * A document citation (`kind: "document"`) is a passage of the user's own
 * PDF: it shows the filename and page, and has no link, because there is no
 * URL and the original file is not kept (ADR-0015).
 */

import { ExternalLink, FileText } from 'lucide-react'

import type { Citation } from '@/api/types'
import { sourceAnchorId } from '@/features/chat/sourceAnchors'
import { isSafeExternalUrl } from '@/lib/safeUrl'

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' })

// Grey shades for site monograms, all dark enough for white text. A favicon
// would need a request to the site (and the CSP allows none); a lettered tile
// costs nothing, and the shade and letter tell sources apart at a glance.
const MONOGRAM_SHADES = [0.24, 0.31, 0.38, 0.45, 0.52]

function monogram(citation: Citation): { letter: string; shade: number } {
  const key = (citation.kind === 'document' ? citation.title : citation.display_url) || '?'
  const host = key.replace(/^www\./, '')
  let hash = 0
  for (const char of host.split('/')[0] ?? host) hash = (hash * 31 + char.charCodeAt(0)) | 0
  return {
    letter: (host[0] ?? '?').toUpperCase(),
    shade: MONOGRAM_SHADES[Math.abs(hash) % MONOGRAM_SHADES.length] ?? 0.24,
  }
}

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
    <section aria-label="Sources" className="mt-5">
      <h3 className="mb-2.5 flex items-center gap-2 text-xs font-semibold tracking-wider text-muted-foreground uppercase">
        Sources
        <span className="rounded-full bg-muted px-1.5 py-px text-[10px] tabular-nums">
          {citations.length}
        </span>
      </h3>
      <ol className="grid gap-2 sm:grid-cols-2">
        {citations.map((citation) => {
          const isDocument = citation.kind === 'document'
          const date = published(citation.published_at)
          const meta = isDocument
            ? [citation.page ? `Page ${citation.page}` : null, 'your document']
                .filter(Boolean)
                .join(' · ')
            : [citation.display_url, citation.publisher, date].filter(Boolean).join(' · ')
          const mark = monogram(citation)
          return (
            <li
              key={citation.source_id}
              id={sourceAnchorId(anchorPrefix, citation.source_id)}
              tabIndex={-1}
              className="group/source relative flex gap-2.5 rounded-xl border border-border bg-card p-3 text-xs transition-[border-color,box-shadow,transform] duration-200 outline-none hover:-translate-y-0.5 hover:border-ring/40 hover:shadow-glow focus:border-ring focus:ring-4 focus:ring-ring/20 has-[a:focus-visible]:border-ring has-[a:focus-visible]:ring-4 has-[a:focus-visible]:ring-ring/30 motion-reduce:transition-none motion-reduce:hover:translate-y-0"
            >
              <span aria-hidden className="relative shrink-0 self-start">
                {isDocument ? (
                  <span className="flex size-7 items-center justify-center rounded-lg bg-primary/12 text-primary">
                    <FileText className="size-3.5" />
                  </span>
                ) : (
                  <span
                    className="flex size-7 items-center justify-center rounded-lg text-[11px] font-bold text-white"
                    style={{ backgroundColor: `oklch(${mark.shade} 0 0)` }}
                  >
                    {mark.letter}
                  </span>
                )}
                <span className="absolute -right-1.5 -bottom-1.5 flex size-4 items-center justify-center rounded-full bg-foreground text-[9px] font-bold text-background tabular-nums ring-2 ring-card">
                  {citation.ordinal}
                </span>
              </span>
              <div className="min-w-0 flex-1">
                {isDocument ? (
                  <span className="line-clamp-1 text-[13px] font-medium">
                    <span className="sr-only">Source {citation.ordinal}: </span>
                    {citation.title}
                  </span>
                ) : citation.url && isSafeExternalUrl(citation.url) ? (
                  <a
                    href={citation.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    // The link's area covers the card (after:inset-0), so the
                    // whole tile is one click target and one tab stop.
                    className="line-clamp-1 text-[13px] font-medium text-foreground underline-offset-2 after:absolute after:inset-0 after:rounded-xl group-hover/source:text-primary hover:underline focus-visible:outline-none"
                  >
                    <span className="sr-only">Source {citation.ordinal}: </span>
                    {citation.title}
                    <ExternalLink
                      className="ml-1 inline size-3 shrink-0 align-[-1px] opacity-60"
                      aria-hidden
                    />
                    <span className="sr-only"> (opens in a new tab)</span>
                  </a>
                ) : (
                  <span className="line-clamp-1 text-[13px] font-medium">{citation.title}</span>
                )}
                <p className="mt-0.5 truncate text-muted-foreground">{meta}</p>
                {citation.snippet ? (
                  <p className="mt-1.5 line-clamp-2 leading-relaxed text-muted-foreground/90">
                    {citation.snippet}
                  </p>
                ) : null}
              </div>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
