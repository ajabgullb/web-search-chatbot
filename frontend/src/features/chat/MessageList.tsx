/**
 * The thread.
 *
 * Auto-scroll follows one rule from AGENTS.md §9: only scroll if the user is
 * already near the bottom. Someone who has scrolled up to read is not
 * interrupted — a chat that yanks you back down is worse than one that never
 * scrolls at all.
 */

import { lazy, Suspense, useEffect, useLayoutEffect, useRef } from 'react'

import type { ChatMessage } from '@/api/types'
import { Button } from '@/components/ui/Button'
import { CopyButton } from '@/features/chat/CopyButton'
import { cn } from '@/lib/utils'

// Markdown pulls in the parser and the syntax highlighter — together the
// largest thing the app ships. Loading it with the first assistant message
// keeps it out of the bundle a visitor downloads to reach the landing or
// login page, which is the only page most first-time visitors ever see.
const Markdown = lazy(() => import('@/features/chat/Markdown'))

/** How close to the bottom still counts as "following along", in pixels. */
const FOLLOW_THRESHOLD = 120

interface MessageListProps {
  messages: ChatMessage[]
  hasOlder: boolean
  isLoadingOlder: boolean
  onLoadOlder: () => void
}

export function MessageList({ messages, hasOlder, isLoadingOlder, onLoadOlder }: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const wasNearBottom = useRef(true)
  const lastCount = useRef(0)

  // Measure *before* the browser paints the new message, otherwise the check
  // runs against a scroll position the new content has already changed.
  useLayoutEffect(() => {
    const element = scrollRef.current
    if (!element) return
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight
    wasNearBottom.current = distance <= FOLLOW_THRESHOLD
  })

  useEffect(() => {
    const element = scrollRef.current
    if (!element) return
    // Only new messages at the end scroll; loading older history must not.
    const grew = messages.length > lastCount.current
    lastCount.current = messages.length
    if (grew && wasNearBottom.current) {
      element.scrollTop = element.scrollHeight
    }
  }, [messages])

  return (
    <div ref={scrollRef} className="flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6">
        {hasOlder ? (
          <div className="flex justify-center">
            <Button variant="outline" size="sm" onClick={onLoadOlder} disabled={isLoadingOlder}>
              {isLoadingOlder ? 'Loading…' : 'Load earlier messages'}
            </Button>
          </div>
        ) : null}

        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}
      </div>
    </div>
  )
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user'

  return (
    <article
      // Each turn is an article with an accessible name, so a screen reader
      // can navigate the thread turn by turn.
      aria-label={isUser ? 'Your message' : 'Assistant message'}
      className={cn('group flex flex-col gap-1', isUser ? 'items-end' : 'items-start')}
    >
      <div
        className={cn(
          'max-w-[85ch] rounded-lg px-4 py-3 text-sm',
          isUser ? 'bg-primary text-primary-foreground' : 'border border-border bg-card',
        )}
      >
        {isUser ? (
          // User text is not Markdown. Rendering it as Markdown would mangle
          // whatever the user actually typed.
          <p className="whitespace-pre-wrap break-words">{message.content}</p>
        ) : (
          // The fallback is the same text, unformatted: something readable is
          // on screen from the first paint, and plain text cannot be unsafe.
          <Suspense fallback={<p className="whitespace-pre-wrap break-words">{message.content}</p>}>
            <Markdown content={message.content} />
          </Suspense>
        )}
      </div>

      <div className="opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100 motion-reduce:transition-none">
        <CopyButton value={message.content} />
      </div>
    </article>
  )
}
