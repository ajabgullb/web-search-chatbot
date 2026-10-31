/**
 * A single thread: history above, the live turn below it, composer at the
 * bottom.
 */

import { useEffect } from 'react'
import { useParams } from 'react-router'

import { ApiError } from '@/api/errors'
import { Alert } from '@/components/ui/Alert'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { Composer } from '@/features/chat/Composer'
import { DocumentChips } from '@/features/chat/DocumentAttachments'
import { PDF_ACCEPT, useDocumentAttachments } from '@/features/chat/useDocumentAttachments'
import { LiveTurn, UnansweredNotice } from '@/features/chat/LiveTurn'
import { MessageList } from '@/features/chat/MessageList'
import { useConversation, useMessages } from '@/features/chat/queries'
import { isActive } from '@/features/chat/runStore'
import { useLiveRun, useRunStore } from '@/features/chat/useLiveRun'

export function ConversationPage() {
  const { conversationId } = useParams()
  const conversation = useConversation(conversationId)
  const messages = useMessages(conversationId)
  const store = useRunStore()
  const live = useLiveRun(conversationId)
  const attachments = useDocumentAttachments(conversationId)

  const activeRunId = conversation.data?.active_run_id ?? null

  // A run the server says is in progress but this tab has no stream for — the
  // page was reloaded mid-answer, or it was started in another tab. Follow it
  // through the recovery endpoint. `recover` is idempotent, so StrictMode's
  // double effect is harmless.
  useEffect(() => {
    if (!conversationId || !activeRunId) return
    const current = store.get(conversationId)
    if (!current || current.phase === 'completed') store.recover(conversationId, activeRunId)
  }, [conversationId, activeRunId, store])

  if (conversation.isPending) {
    return <FullPageSpinner label="Loading conversation" />
  }

  if (conversation.isError || !conversationId) {
    const missing =
      conversation.error instanceof ApiError && conversation.error.is('conversation_not_found')
    return (
      <main className="flex flex-1 items-center justify-center p-6">
        <Alert
          variant="error"
          className="max-w-md"
          title={missing ? 'Conversation not found' : 'Something went wrong'}
        >
          {missing
            ? 'It may have been deleted. Pick another conversation from the sidebar.'
            : 'The conversation could not be loaded. Try again in a moment.'}
        </Alert>
      </main>
    )
  }

  const history = messages.data?.items ?? []
  const last = history.at(-1)
  const running = isActive(live)
  // The last question has no answer and nothing is working on it: offer a
  // retry, unless the live turn already shows one for this very message.
  const unanswered =
    last?.role === 'user' && !running && !activeRunId && live?.userMessageId !== last.id

  return (
    <main className="flex min-h-0 flex-1 flex-col">
      <div className="glass sticky top-0 z-10 border-b border-border/70 px-4 py-3">
        <h1 className="mx-auto max-w-3xl truncate text-sm font-semibold tracking-tight">
          {conversation.data.title}
        </h1>
      </div>

      <MessageList
        messages={history}
        hasOlder={messages.hasNextPage}
        isLoadingOlder={messages.isFetchingNextPage}
        onLoadOlder={() => void messages.fetchNextPage()}
        followKey={`${live?.phase}:${live?.text.length}:${live?.searches.length}:${live?.citations.length}`}
      >
        {live ? <LiveTurn run={live} onRetry={() => store.retry(conversationId)} /> : null}
        {unanswered ? (
          <UnansweredNotice onRetry={() => store.retry(conversationId, last.id)} />
        ) : null}
      </MessageList>

      {/* Progress for assistive tech: one sentence per phase change, never a
          token-by-token recital of the answer. */}
      <div role="status" aria-live="polite" className="sr-only">
        {live?.announcement}
      </div>

      <Composer
        onSubmit={(content) => store.send(conversationId, content)}
        running={running}
        stopping={live?.stopping ?? false}
        onStop={() => void store.stop(conversationId)}
        onAttach={attachments.attach}
        attaching={attachments.uploading}
        accept={PDF_ACCEPT}
        attachments={<DocumentChips attachments={attachments} running={running} />}
      />
    </main>
  )
}
