/**
 * TanStack Query bindings for the conversation domain.
 *
 * Query keys are built from one factory so an invalidation cannot miss a
 * cache entry because two call sites spelled the key differently.
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import * as chatApi from '@/features/chat/api'

export const chatKeys = {
  conversations: ['conversations'] as const,
  conversation: (id: string) => ['conversations', id] as const,
  messages: (id: string) => ['conversations', id, 'messages'] as const,
  documents: (id: string) => ['conversations', id, 'documents'] as const,
}

/** How often to re-check while a document is still being prepared. */
export const DOCUMENT_POLL_MS = 1_500

/**
 * The sidebar list.
 *
 * `next_cursor` is null exactly when the server has nothing more, so the page
 * param comes straight from the response and the hook never has to reason
 * about counts or page sizes.
 */
export function useConversations() {
  return useInfiniteQuery({
    queryKey: chatKeys.conversations,
    queryFn: ({ pageParam }: { pageParam: string | undefined }) =>
      chatApi.listConversations(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    select: (data) => ({
      pages: data.pages,
      items: data.pages.flatMap((page) => page.items),
    }),
  })
}

export function useConversation(id: string | undefined) {
  return useQuery({
    queryKey: chatKeys.conversation(id ?? ''),
    queryFn: () => chatApi.getConversation(id as string),
    enabled: id !== undefined,
  })
}

/**
 * Thread history.
 *
 * Pages walk *backwards*: page one is the newest turns, and fetching the next
 * page reaches further into the past. Flattening therefore prepends each new
 * page rather than appending it.
 */
export function useMessages(id: string | undefined) {
  return useInfiniteQuery({
    queryKey: chatKeys.messages(id ?? ''),
    queryFn: ({ pageParam }: { pageParam: string | undefined }) =>
      chatApi.listMessages(id as string, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: id !== undefined,
    select: (data) => ({
      pages: data.pages,
      items: data.pages
        .slice()
        .reverse()
        .flatMap((page) => page.items),
    }),
  })
}

/**
 * The conversation's documents.
 *
 * Polls only while one is still `processing`: embedding happens in the
 * background after the upload returns, and there is no event stream for it.
 * Once everything is `ready` or `failed` the list goes quiet.
 */
export function useDocuments(conversationId: string | undefined) {
  return useQuery({
    queryKey: chatKeys.documents(conversationId ?? ''),
    queryFn: () => chatApi.listDocuments(conversationId as string),
    enabled: conversationId !== undefined,
    refetchInterval: (query) =>
      query.state.data?.items.some((document) => document.status === 'processing')
        ? DOCUMENT_POLL_MS
        : false,
  })
}

export function useUploadDocument(conversationId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (file: File) => chatApi.uploadDocument(conversationId, file),
    // Refetched rather than patched: a duplicate upload returns an existing
    // document, and the server's list is the one source of order.
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: chatKeys.documents(conversationId) }),
  })
}

export function useDeleteDocument(conversationId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (documentId: string) => chatApi.deleteDocument(conversationId, documentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: chatKeys.documents(conversationId) })
      // The server deleted every citation of the document with it. Refetch
      // the thread so its source cards go now, not on the next reload.
      void queryClient.invalidateQueries({ queryKey: chatKeys.messages(conversationId) })
    },
  })
}

export function useCreateConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (title?: string) => chatApi.createConversation(title),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: chatKeys.conversations }),
  })
}

export function useRenameConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      chatApi.renameConversation(id, title),
    onSuccess: (conversation) => {
      // A rename bumps updated_at, which is the list's sort key, so the list
      // has to be refetched rather than patched in place.
      queryClient.setQueryData(chatKeys.conversation(conversation.id), conversation)
      void queryClient.invalidateQueries({ queryKey: chatKeys.conversations })
    },
  })
}

export function useDeleteConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => chatApi.deleteConversation(id),
    onSuccess: (_result, id) => {
      queryClient.removeQueries({ queryKey: chatKeys.conversation(id) })
      void queryClient.invalidateQueries({ queryKey: chatKeys.conversations })
    },
  })
}
