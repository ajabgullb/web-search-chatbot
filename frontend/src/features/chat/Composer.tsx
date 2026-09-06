/**
 * The message input.
 *
 * Keyboard contract: Enter sends, Shift+Enter inserts a newline. That is what
 * chat users expect, but it means the textarea's default behaviour has to be
 * prevented on the send path — and only there, so IME composition and
 * Shift+Enter still work normally.
 */

import { SendHorizonal } from 'lucide-react'
import { useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'

import { Button } from '@/components/ui/Button'
import { COUNTER_VISIBLE_FROM, MAX_MESSAGE_CHARACTERS } from '@/features/chat/constants'
import { cn } from '@/lib/utils'

interface ComposerProps {
  onSubmit: (content: string) => void
  /** True while a run is active: the composer accepts no new message. */
  busy?: boolean
  disabledReason?: string
}

export function Composer({ onSubmit, busy = false, disabledReason }: ComposerProps) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const trimmed = value.trim()
  const tooLong = value.length > MAX_MESSAGE_CHARACTERS
  const canSend = trimmed.length > 0 && !tooLong && !busy

  function send() {
    if (!canSend) return
    onSubmit(trimmed)
    setValue('')
    // Height is set from content, so it has to be reset with the value.
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // `isComposing` guards IME input: for many keyboards Enter commits a
    // candidate word, and sending there would truncate what was being typed.
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      send()
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    send()
  }

  return (
    <form onSubmit={handleSubmit} className="border-t border-border bg-background p-4">
      <div className="mx-auto flex max-w-3xl flex-col gap-2">
        <div className="flex items-end gap-2 rounded-lg border border-input bg-background p-2 focus-within:ring-2 focus-within:ring-ring">
          <label htmlFor="composer" className="sr-only">
            Message
          </label>
          <textarea
            id="composer"
            ref={textareaRef}
            rows={1}
            value={value}
            disabled={busy}
            onChange={(event) => {
              setValue(event.target.value)
              // Grow with the content up to a ceiling, then scroll.
              const element = event.target
              element.style.height = 'auto'
              element.style.height = `${Math.min(element.scrollHeight, 200)}px`
            }}
            onKeyDown={handleKeyDown}
            placeholder={busy ? 'Waiting for the answer…' : 'Ask anything…'}
            aria-describedby="composer-help"
            aria-invalid={tooLong || undefined}
            className="max-h-50 min-h-9 flex-1 resize-none bg-transparent px-1 py-1.5 text-sm outline-none disabled:opacity-60"
          />
          <Button type="submit" size="icon" disabled={!canSend} aria-label="Send message">
            <SendHorizonal className="size-4" aria-hidden />
          </Button>
        </div>

        <div className="flex items-center justify-between gap-4 text-xs text-muted-foreground">
          <p id="composer-help">{disabledReason ?? 'Enter to send, Shift+Enter for a new line.'}</p>
          {value.length >= COUNTER_VISIBLE_FROM ? (
            <p
              // Announced politely so a screen-reader user learns they are near
              // the limit without the count interrupting every keystroke.
              aria-live="polite"
              className={cn('tabular-nums', tooLong && 'font-medium text-destructive')}
            >
              {value.length.toLocaleString()} / {MAX_MESSAGE_CHARACTERS.toLocaleString()}
            </p>
          ) : null}
        </div>
      </div>
    </form>
  )
}
