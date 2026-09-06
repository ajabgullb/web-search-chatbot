/**
 * A single thread: history above, composer below.
 */

import { useState } from 'react'
import { useParams } from 'react-router'

import { ApiError } from '@/api/errors'
import { Alert } from '@/components/ui/Alert'
import { FullPageSpinner } from '@/components/FullPageSpinner'
import { Composer } from '@/features/chat/Composer'
import { MessageList } from '@/features/chat/MessageList'
import { useConversation, useMessages } from '@/features/chat/queries'

export function ConversationPage() {
  const { conversationId } = useParams()
  const conversation = useConversation(conversationId)
  const messages = useMessages(conversationId)
  const [notice, setNotice] = useState<string | null>(null)

  if (conversation.isPending) {
    return <FullPageSpinner label="Loading conversation" />
  }

  if (conversation.isError) {
    const missing =
      conversation.error instanceof ApiError && conversation.error.is('conversation_not_found')
    return (
      <main className="flex flex-1 items-center justify-center p-6">
        <Alert variant="error" title={missing ? 'Conversation not found' : 'Something went wrong'}>
          {missing
            ? 'It may have been deleted. Pick another conversation from the sidebar.'
            : 'The conversation could not be loaded. Try again in a moment.'}
        </Alert>
      </main>
    )
  }

  return (
    <main className="flex min-h-0 flex-1 flex-col">
      <div className="border-b border-border px-4 py-3">
        <h1 className="truncate text-sm font-semibold">{conversation.data.title}</h1>
      </div>

      <MessageList
        messages={messages.data?.items ?? []}
        hasOlder={messages.hasNextPage}
        isLoadingOlder={messages.isFetchingNextPage}
        onLoadOlder={() => void messages.fetchNextPage()}
      />

      {/* Status announcements for assistive tech; Phase 7 streams run progress
          through the same region. */}
      <div role="status" aria-live="polite" className="sr-only">
        {notice}
      </div>
      {notice ? (
        <div className="mx-auto w-full max-w-3xl px-4">
          <Alert variant="info" title="Not connected yet">
            {notice}
          </Alert>
        </div>
      ) : null}

      <Composer
        onSubmit={() =>
          setNotice(
            'The agent arrives in the next phase, so this message was not sent. Conversations, history, and the composer are working.',
          )
        }
        disabledReason="Enter to send, Shift+Enter for a new line. Answering arrives with the agent."
      />
    </main>
  )
}
