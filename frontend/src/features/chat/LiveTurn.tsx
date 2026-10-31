/**
 * The turn in progress: the optimistic question and the answer as it streams.
 *
 * Everything here is temporary. The optimistic question is replaced by the
 * stored one when `run.started` confirms it; the streamed answer disappears
 * in the same update that puts the canonical message into the history. What
 * stays on screen after a failure or a stop is the partial text, clearly
 * marked, with a way to try again.
 */

import { AlertCircle, Check, FileText, Globe, LoaderCircle, RotateCcw } from 'lucide-react'
import { useDeferredValue } from 'react'
import type { ReactNode } from 'react'

import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { AnswerText, AssistantBubble, UserBubble } from '@/features/chat/MessageList'
import type { LiveDocuments, LiveRun, LiveSearch } from '@/features/chat/runStore'
import { isActive } from '@/features/chat/runStore'
import { SourceList } from '@/features/chat/SourceList'
import { cn } from '@/lib/utils'

function activityLabel(run: LiveRun): string {
  if (run.stopping) return 'Stopping…'
  if (run.phase === 'sending') return 'Sending…'
  if (run.phase === 'recovering') return 'Reconnecting to the answer…'
  switch (run.status) {
    case 'reading_documents':
      return 'Reading your documents…'
    case 'searching':
      return 'Searching the web…'
    case 'writing':
      return 'Writing…'
    default:
      return 'Thinking…'
  }
}

/**
 * One step of the agent's work, as a row in a small timeline: a spinner
 * while it runs, a check when it is done, a warning if it failed.
 */
function Step({
  state,
  icon,
  children,
}: {
  state: 'running' | 'done' | 'failed'
  icon: ReactNode
  children: ReactNode
}) {
  return (
    <li className="animate-rise flex min-w-0 items-center gap-2">
      <span
        aria-hidden
        className={cn(
          'flex size-5 shrink-0 items-center justify-center rounded-full',
          state === 'done' && 'bg-foreground text-background',
          state === 'failed' && 'bg-destructive/12 text-destructive',
          state === 'running' && 'bg-primary/12 text-primary',
        )}
      >
        {state === 'running' ? (
          <LoaderCircle className="size-3 animate-spin motion-reduce:animate-none" />
        ) : state === 'done' ? (
          <Check className="size-3" strokeWidth={3} />
        ) : (
          <AlertCircle className="size-3" />
        )}
      </span>
      <span aria-hidden className="shrink-0 text-muted-foreground/70">
        {icon}
      </span>
      {children}
    </li>
  )
}

function DocumentProgress({ documents }: { documents: LiveDocuments | null }) {
  if (documents === null) return null
  const icon = <FileText className="size-3.5" />
  if (documents.failed) {
    return (
      <Step state="failed" icon={icon}>
        Your documents could not be read for this answer
      </Step>
    )
  }
  if (documents.documents.length === 0) {
    return (
      <Step state="done" icon={icon}>
        Nothing in your documents matched this question
      </Step>
    )
  }
  return (
    <>
      {documents.documents.map((document) => (
        <Step key={document.document_id} state="done" icon={icon}>
          <span className="truncate">Read {document.filename}</span>
          <span className="shrink-0">
            · {document.chunk_count} {document.chunk_count === 1 ? 'passage' : 'passages'}
          </span>
        </Step>
      ))}
    </>
  )
}

function SearchProgress({ searches }: { searches: LiveSearch[] }) {
  return (
    <>
      {searches.map((search, index) => (
        <Step
          key={`${search.query}-${index}`}
          state={!search.done ? 'running' : search.failed ? 'failed' : 'done'}
          icon={<Globe className="size-3.5" />}
        >
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
        </Step>
      ))}
    </>
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
        <UserBubble content={run.pendingContent} animate>
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
          animate
        >
          {active ? (
            <p className="mb-3 inline-flex items-center gap-2 rounded-full border border-border bg-card px-3 py-1 text-xs font-medium">
              <span
                aria-hidden
                className="size-1.5 animate-pulse rounded-full bg-primary motion-reduce:animate-none"
              />
              <span className="text-shimmer">{activityLabel(run)}</span>
            </p>
          ) : null}

          {run.documents !== null || run.searches.length > 0 ? (
            <ul className="mb-4 flex flex-col gap-1.5 border-l-2 border-border pl-3 text-xs text-muted-foreground">
              <DocumentProgress documents={run.documents} />
              <SearchProgress searches={run.searches} />
            </ul>
          ) : null}

          {text ? (
            <div
              className={cn(
                // The caret only while words are arriving: thinking and
                // searching show the status pill instead.
                active && run.phase === 'streaming' && 'stream-caret',
                !active && 'opacity-80',
              )}
            >
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
            <Button variant="outline" size="sm" className="mt-3 rounded-full" onClick={onRetry}>
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
      <Button variant="outline" size="sm" className="mt-3 rounded-full" onClick={onRetry}>
        <RotateCcw className="size-3.5" aria-hidden />
        Retry
      </Button>
    </AssistantBubble>
  )
}
