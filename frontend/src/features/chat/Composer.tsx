/**
 * The message input.
 *
 * Keyboard contract: Enter sends, Shift+Enter inserts a newline. That is what
 * chat users expect, but it means the textarea's default behaviour has to be
 * prevented on the send path — and only there, so IME composition and
 * Shift+Enter still work normally.
 */

import { ArrowUp, Paperclip, Square } from 'lucide-react'
import { useRef, useState } from 'react'
import type { ChangeEvent, FormEvent, KeyboardEvent, ReactNode } from 'react'

import { Button } from '@/components/ui/Button'
import { COUNTER_VISIBLE_FROM, MAX_MESSAGE_CHARACTERS } from '@/features/chat/constants'
import { cn } from '@/lib/utils'

interface ComposerProps {
  onSubmit: (content: string) => void
  /**
   * True while a run is active. The composer is *limited*, not disabled: the
   * next message can be drafted, but not sent, and the send button becomes a
   * stop button. Disabling the textarea would throw a keyboard user's focus
   * out of the box they are typing in.
   */
  running?: boolean
  onStop?: () => void
  stopping?: boolean
  /**
   * Called with a file the user picked. Without it there is no attach button.
   * Attaching does not send a message: the file belongs to the conversation.
   */
  onAttach?: (file: File) => void
  /** True while a file is uploading; one at a time. */
  attaching?: boolean
  /** File types the picker offers, as for `<input accept>`. */
  accept?: string
  /** Rendered inside the box, above the text: what is attached. */
  attachments?: ReactNode
  /**
   * `docked` sits at the foot of a thread; `hero` is the larger, free-standing
   * box on the chat home, focused on arrival because typing is the one thing
   * that page is for.
   */
  variant?: 'docked' | 'hero'
  placeholder?: string
  /** True while the conversation behind a hero send is being created. */
  busy?: boolean
}

export function Composer({
  onSubmit,
  running = false,
  onStop,
  stopping = false,
  onAttach,
  attaching = false,
  accept,
  attachments,
  variant = 'docked',
  placeholder,
  busy = false,
}: ComposerProps) {
  const hero = variant === 'hero'
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  function handleFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    // Cleared, so picking the same file again (after removing it) still fires
    // a change event.
    event.target.value = ''
    if (file && onAttach) onAttach(file)
  }

  const trimmed = value.trim()
  const tooLong = value.length > MAX_MESSAGE_CHARACTERS
  const canSend = trimmed.length > 0 && !tooLong && !running && !busy

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
    <form
      onSubmit={handleSubmit}
      className={cn(
        hero
          ? 'w-full'
          : // Fades the thread out behind the box instead of a hard rule.
            'bg-gradient-to-t from-background from-60% to-transparent px-4 pt-2 pb-4',
      )}
    >
      <div className={cn('mx-auto flex flex-col gap-2', hero ? 'max-w-2xl' : 'max-w-3xl')}>
        <div
          className={cn(
            'flex flex-col gap-2 border border-input bg-card p-2 shadow-sm',
            'transition-[border-color,box-shadow] duration-200 motion-reduce:transition-none',
            'focus-within:border-ring/60 focus-within:shadow-glow focus-within:ring-4 focus-within:ring-ring/10',
            hero ? 'rounded-3xl p-3' : 'rounded-2xl',
          )}
        >
          {attachments}
          <div className="flex items-end gap-2">
            {onAttach ? (
              <>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept={accept}
                  onChange={handleFile}
                  // Hidden, and out of the tab order: the button is the control.
                  hidden
                  tabIndex={-1}
                  data-testid="composer-file-input"
                />
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={attaching}
                  aria-label={attaching ? 'Uploading document' : 'Attach a PDF'}
                  title="Attach a PDF"
                  className="shrink-0 text-muted-foreground"
                >
                  <Paperclip className="size-4" aria-hidden />
                </Button>
              </>
            ) : null}
            <label htmlFor="composer" className="sr-only">
              Message
            </label>
            <textarea
              id="composer"
              ref={textareaRef}
              rows={1}
              value={value}
              onChange={(event) => {
                setValue(event.target.value)
                // Grow with the content up to a ceiling, then scroll.
                const element = event.target
                element.style.height = 'auto'
                element.style.height = `${Math.min(element.scrollHeight, 200)}px`
              }}
              onKeyDown={handleKeyDown}
              placeholder={running ? 'Write your next message…' : (placeholder ?? 'Ask anything…')}
              // Only the hero box: on the chat home typing is the page's one
              // task, while a docked box must not steal focus on navigation.
              autoFocus={hero}
              aria-describedby="composer-help"
              aria-invalid={tooLong || undefined}
              className={cn(
                'max-h-50 flex-1 resize-none bg-transparent px-1.5 outline-none placeholder:text-muted-foreground/80',
                hero ? 'min-h-14 py-2 text-base' : 'min-h-9 py-1.5 text-[15px]',
              )}
            />
            {running ? (
              <Button
                type="button"
                size="icon"
                variant="outline"
                className="shrink-0 rounded-full"
                onClick={onStop}
                disabled={stopping || !onStop}
                aria-label={stopping ? 'Stopping' : 'Stop answering'}
              >
                <Square className="size-3.5 fill-current" aria-hidden />
              </Button>
            ) : (
              <Button
                type="submit"
                size="icon"
                variant="ink"
                className={cn(
                  'shrink-0 rounded-full',
                  // Grey until there is something to send, so the colour
                  // itself says "ready".
                  !canSend && 'bg-none bg-muted text-muted-foreground shadow-none',
                )}
                disabled={!canSend}
                aria-label="Send message"
              >
                <ArrowUp className="size-4" strokeWidth={2.5} aria-hidden />
              </Button>
            )}
          </div>
        </div>

        <div className="flex items-center justify-between gap-4 px-2 text-xs text-muted-foreground">
          <p id="composer-help">
            {running
              ? 'The assistant is answering. You can draft your next message, or stop the answer.'
              : 'Enter to send, Shift+Enter for a new line.'}
          </p>
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
