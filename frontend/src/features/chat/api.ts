/**
 * Conversation endpoint wrappers.
 *
 * Thin and typed, like the auth ones. Cursors are opaque strings the server
 * produced — this layer passes them straight back and never constructs one.
 */

import { request } from '@/api/client'
import type {
  ChatDocument,
  Conversation,
  ConversationDetail,
  ConversationPage,
  ConversationUpdate,
  DocumentList,
  MessagePage,
  Run,
} from '@/api/types'

export const PAGE_SIZE = 20

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

export function listConversations(cursor?: string): Promise<ConversationPage> {
  return request<ConversationPage>(`/conversations${query({ cursor, limit: PAGE_SIZE })}`)
}

export function getConversation(id: string): Promise<ConversationDetail> {
  return request<ConversationDetail>(`/conversations/${id}`)
}

export function createConversation(title?: string): Promise<Conversation> {
  return request<Conversation>('/conversations', {
    method: 'POST',
    body: { title: title ?? null },
  })
}

export function renameConversation(id: string, title: string): Promise<Conversation> {
  const body: ConversationUpdate = { title }
  return request<Conversation>(`/conversations/${id}`, { method: 'PATCH', body })
}

export function deleteConversation(id: string): Promise<void> {
  return request<void>(`/conversations/${id}`, { method: 'DELETE' })
}

export function listMessages(id: string, before?: string): Promise<MessagePage> {
  return request<MessagePage>(`/conversations/${id}/messages${query({ before, limit: PAGE_SIZE })}`)
}

export function getRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}`)
}

export function cancelRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/cancel`, { method: 'POST' })
}

export function listDocuments(conversationId: string): Promise<DocumentList> {
  return request<DocumentList>(`/conversations/${conversationId}/documents`)
}

/**
 * Upload a PDF. Resolves with `status: "processing"`; the document becomes
 * `ready` (or `failed`) in the background, so callers poll the list.
 */
export function uploadDocument(conversationId: string, file: File): Promise<ChatDocument> {
  const form = new FormData()
  form.append('file', file, file.name)
  return request<ChatDocument>(`/conversations/${conversationId}/documents`, {
    method: 'POST',
    form,
  })
}

export function deleteDocument(conversationId: string, documentId: string): Promise<void> {
  return request<void>(`/conversations/${conversationId}/documents/${documentId}`, {
    method: 'DELETE',
  })
}
