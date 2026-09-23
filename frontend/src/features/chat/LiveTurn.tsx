/**
 * The turn in progress: the optimistic question and the answer as it streams.
 *
 * Everything here is temporary. The optimistic question is replaced by the
 * stored one when `run.started` confirms it; the streamed answer disappears
 * in the same update that puts the canonical message into the history. What
 * stays on screen after a failure or a stop is the partial text, clearly
 * marked, with a way to try again.
 */

import { AlertCircle, Globe, Loader2, RotateCcw } from 'lucide-react'
import { useDeferredValue } from 'react'

import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { AnswerText, AssistantBubble, UserBubble } from '@/features/chat/MessageList'
import type { LiveRun, LiveSearch } from '@/features/chat/runStore'
import { isActive } from '@/features/chat/runStore'
import { SourceList } from '@/features/chat/SourceList'

function activityLabel(run: LiveRun): string {
  if (run.stopping) return 'Stopping…'
  if (run.phase === 'sending') return 'Sending…'
  if (run.phase === 'recovering') return 'Reconnecting to the answer…'
  switch (run.status) {
    case 'searching':
      return 'Searching the web…'
    case 'writing':
      return 'Writing…'
    default:
      return 'Thinking…'
  }
}

function SearchProgress({ searches }: { searches: LiveSearch[] }) {
  if (searches.length === 0) return null
  return (
    <ul className="mb-2 flex flex-col gap-1 text-xs text-muted-foreground">
      {searches.map((search, index) => (
        <li key={`${search.query}-${index}`} className="flex items-center gap-1.5">
          <Globe className="size-3 shrink-0" aria-hidden />
          <span className="truncate">
            {search.done ? 'Searched' : 'Searching'} “{search.query}”
          </span>
          {search.done ? (
            <span className="shrink-0">
              {search.failed
                ? '· unavailable'
                : `· ${search.sources.length} ${search.sources.length === 1 ? 'source' : 'sources'}`}
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  )
}

interface LiveTurnProps {
  run: LiveRun
  onRetry: () => void
}

export function LiveTurn({ run, onRetry }: LiveTurnProps) {
  // Markdown re-parses on every delta. Deferring the text lets React skip
  // intermediate renders under load instead of letting typing lag behind.
  const text = useDeferredValue(run.text)
  const active = isActive(run)
  const anchorPrefix = `live-${run.runId ?? 'pending'}`

  const showAnswer =
    run.phase === 'sending' ||
    run.phase === 'streaming' ||
    run.phase === 'recovering' ||
    run.phase === 'failed' ||
    run.phase === 'cancelled'
  // Retrying needs the stored question. A send aborted before the server
  // confirmed it has none; the history offers the retry if it was stored.
  const canRetry = run.userMessageId !== null && (run.phase === 'cancelled' || run.error?.retryable)

  return (
    <>
      {run.pendingContent !== null ? (
        <UserBubble content={run.pendingContent}>
          {run.phase === 'rejected' ? (
            <div className="flex flex-col items-end gap-1 text-xs">
              <p className="flex items-center gap-1 text-destructive">
                <AlertCircle className="size-3.5" aria-hidden />
                Not sent. {run.error?.message}
              </p>
              <Button variant="outline" size="sm" onClick={onRetry}>
                <RotateCcw className="size-3.5" aria-hidden />
                Try again
              </Button>
            </div>
          ) : null}
        </UserBubble>
      ) : null}

      {showAnswer ? (
        <AssistantBubble
          label={active ? 'Assistant is answering' : 'Assistant message'}
          busy={active}
        >
          {active ? (
            <p className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden />
              {activityLabel(run)}
            </p>
          ) : null}

          <SearchProgress searches={run.searches} />

          {text ? (
            <div className={active ? undefined : 'opacity-80'}>
              <AnswerText content={text} citations={run.citations} anchorPrefix={anchorPrefix} />
            </div>
          ) : null}

          <SourceList citations={run.citations} anchorPrefix={anchorPrefix} />

          {run.phase === 'failed' ? (
            <Alert variant="error" className="mt-3" title="The answer failed">
              {run.error?.message}
            </Alert>
          ) : null}
          {run.phase === 'cancelled' ? (
            <p className="mt-2 text-xs text-muted-foreground">
              {run.cancelReason === 'client_disconnected'
                ? 'The connection closed before the answer finished.'
                : 'Stopped.'}
            </p>
          ) : null}

          {!active && canRetry ? (
            <Button variant="outline" size="sm" className="mt-3" onClick={onRetry}>
              <RotateCcw className="size-3.5" aria-hidden />
              Retry
            </Button>
          ) : null}
        </AssistantBubble>
      ) : null}
    </>
  )
}

/** A question in the history that never got an answer — after a reload, say. */
export function UnansweredNotice({ onRetry }: { onRetry: () => void }) {
  return (
    <AssistantBubble label="No answer">
      <p className="text-muted-foreground">This message did not get an answer.</p>
      <Button variant="outline" size="sm" className="mt-3" onClick={onRetry}>
        <RotateCcw className="size-3.5" aria-hidden />
        Retry
      </Button>
    </AssistantBubble>
  )
}
