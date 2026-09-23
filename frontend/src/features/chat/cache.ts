/**
 * Reconciling streamed results with the TanStack Query cache.
 *
 * The thread's history lives in one infinite query. When the server confirms a
 * message — the user's own in `run.started`, the answer in `message.completed`
 * — it is merged into that cache directly, so the canonical copy appears in
 * the same render that removes the streamed one. Refetching instead would
 * leave a frame with both, or neither.
 */

import type { InfiniteData, QueryClient } from '@tanstack/react-query'

import type { ChatMessage, MessagePage } from '@/api/types'
import { chatKeys } from '@/features/chat/queries'

/**
 * Merge messages into the newest page of the thread.
 *
 * Idempotent by message id, and ordered by sequence number, so replaying an
 * event (or merging a message a refetch already brought in) changes nothing.
 * If the history has not been loaded yet there is nothing to merge into, and
 * the first fetch will bring the messages anyway.
 */
export function mergeMessages(
  queryClient: QueryClient,
  conversationId: string,
  messages: ChatMessage[],
): void {
  queryClient.setQueryData<InfiniteData<MessagePage, string | undefined>>(
    chatKeys.messages(conversationId),
    (data) => {
      const newest = data?.pages[0]
      if (!data || !newest) return data

      const byId = new Map(newest.items.map((message) => [message.id, message]))
      for (const message of messages) byId.set(message.id, message)
      const items = [...byId.values()].sort((a, b) => a.sequence_number - b.sequence_number)

      return { ...data, pages: [{ ...newest, items }, ...data.pages.slice(1)] }
    },
  )
}

/**
 * The sidebar and the header change when a thread is used: the first message
 * titles it, and every run moves it to the top of the list.
 */
export function refreshConversation(queryClient: QueryClient, conversationId: string): void {
  void queryClient.invalidateQueries({ queryKey: chatKeys.conversations, exact: true })
  void queryClient.invalidateQueries({
    queryKey: chatKeys.conversation(conversationId),
    exact: true,
  })
}
