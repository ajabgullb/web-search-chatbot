/**
 * The thread.
 *
 * Auto-scroll follows one rule from AGENTS.md §9: only scroll if the user is
 * already near the bottom. Someone who has scrolled up to read is not
 * interrupted — a chat that yanks you back down is worse than one that never
 * scrolls at all.
 *
 * "Near the bottom" is decided by scroll events, not by measuring after each
 * render. Content growth never fires a scroll event, so a streamed answer
 * getting longer cannot flip the decision; only the reader moving can. When
 * the reader scrolls up, following stops; when they return to the bottom, it
 * resumes.
 */

import { lazy, Suspense, useEffect, useLayoutEffect, useRef } from 'react'
import type { ReactNode } from 'react'

import type { ChatMessage } from '@/api/types'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { CopyButton } from '@/features/chat/CopyButton'
import { SourceList } from '@/features/chat/SourceList'
import { cn } from '@/lib/utils'

// Markdown pulls in the parser and the syntax highlighter — together the
// largest thing the app ships. Loading it with the first assistant message
// keeps it out of the bundle a visitor downloads to reach the landing or
// login page, which is the only page most first-time visitors ever see.
export const Markdown = lazy(() => import('@/features/chat/Markdown'))

/** How close to the bottom still counts as "following along", in pixels. */
const FOLLOW_THRESHOLD = 120

interface MessageListProps {
  messages: ChatMessage[]
  hasOlder: boolean
  isLoadingOlder: boolean
  onLoadOlder: () => void
  /** The live turn, rendered after the history. */
  children?: ReactNode
  /** Changes whenever the bottom of the thread grows; drives following. */
  followKey?: unknown
}

export function MessageList({
  messages,
  hasOlder,
  isLoadingOlder,
  onLoadOlder,
  children,
  followKey,
}: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const following = useRef(true)

  function handleScroll() {
    const element = scrollRef.current
    if (!element) return
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight
    following.current = distance <= FOLLOW_THRESHOLD
  }

  const lastId = messages.at(-1)?.id
  // Layout effect: jump before the browser paints, so growing text never
  // flashes one frame below the fold.
  useLayoutEffect(() => {
    const element = scrollRef.current
    if (element && following.current) element.scrollTop = element.scrollHeight
  }, [lastId, followKey])

  // Content also grows in renders `followKey` cannot see: the streamed text is
  // deferred and may paint a render later, and the lazy Markdown chunk swaps
  // plain text for formatted text of a different height. Watching the actual
  // size catches all of them. (jsdom has no ResizeObserver; the effect above
  // is what tests exercise.)
  useEffect(() => {
    const element = scrollRef.current
    const content = contentRef.current
    if (!element || !content || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      if (following.current) element.scrollTop = element.scrollHeight
    })
    observer.observe(content)
    return () => observer.disconnect()
  }, [])

  return (
    <div
      ref={scrollRef}
      onScroll={handleScroll}
      // `relative` makes the thread the containing block for the absolutely
      // positioned `sr-only` labels inside it. Without it they are placed
      // against the page instead, escape this overflow, and give the whole
      // page a second scrollbar.
      className="relative flex-1 overflow-y-auto"
      data-testid="thread"
    >
      <div ref={contentRef} className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6">
        {hasOlder ? (
          <div className="flex justify-center">
            <Button variant="outline" size="sm" onClick={onLoadOlder} disabled={isLoadingOlder}>
              {isLoadingOlder ? 'Loading…' : 'Load earlier messages'}
            </Button>
          </div>
        ) : null}

        {messages.map((message) =>
          message.role === 'user' ? (
            <UserBubble key={message.id} content={message.content} />
          ) : (
            <AssistantMessage key={message.id} message={message} />
          ),
        )}

        {children}
      </div>
    </div>
  )
}

export function UserBubble({ content, children }: { content: string; children?: ReactNode }) {
  return (
    <article
      // Each turn is an article with an accessible name, so a screen reader
      // can navigate the thread turn by turn.
      aria-label="Your message"
      className="group flex flex-col items-end gap-1"
    >
      <div className="max-w-[85ch] rounded-lg bg-primary px-4 py-3 text-sm text-primary-foreground">
        {/* User text is not Markdown. Rendering it as Markdown would mangle
            whatever the user actually typed. */}
        <p className="break-words whitespace-pre-wrap">{content}</p>
      </div>
      {children}
    </article>
  )
}

export function AssistantBubble({
  children,
  className,
  label = 'Assistant message',
  busy = false,
}: {
  children: ReactNode
  className?: string
  label?: string
  busy?: boolean
}) {
  return (
    <article
      aria-label={label}
      aria-busy={busy || undefined}
      className="group flex flex-col items-start gap-1"
    >
      <div
        className={cn(
          'w-full max-w-[85ch] rounded-lg border border-border bg-card px-4 py-3 text-sm',
          className,
        )}
      >
        {children}
      </div>
    </article>
  )
}

/** Rendered answer text: Markdown once loaded, the same text plain until then. */
export function AnswerText({
  content,
  citations,
  anchorPrefix,
}: {
  content: string
  citations: ChatMessage['citations']
  anchorPrefix: string
}) {
  return (
    // The fallback is the same text, unformatted: something readable is on
    // screen from the first paint, and plain text cannot be unsafe.
    <Suspense fallback={<p className="break-words whitespace-pre-wrap">{content}</p>}>
      <Markdown content={content} citations={citations ?? []} anchorPrefix={anchorPrefix} />
    </Suspense>
  )
}

function AssistantMessage({ message }: { message: ChatMessage }) {
  const citations = message.citations ?? []
  const anchorPrefix = `msg-${message.id}`

  return (
    <div className="group flex flex-col items-start gap-1">
      <AssistantBubble>
        <AnswerText content={message.content} citations={citations} anchorPrefix={anchorPrefix} />
        {message.degraded && message.notice ? (
          <Alert variant="info" className="mt-3">
            {message.notice}
          </Alert>
        ) : null}
        <SourceList citations={citations} anchorPrefix={anchorPrefix} />
      </AssistantBubble>
      <div className="opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100 motion-reduce:transition-none">
        <CopyButton value={message.content} />
      </div>
    </div>
  )
}
