/**
 * Conversation endpoint wrappers.
 *
 * Thin and typed, like the auth ones. Cursors are opaque strings the server
 * produced — this layer passes them straight back and never constructs one.
 */

import { request } from '@/api/client'
import type { Conversation, ConversationPage, ConversationUpdate, MessagePage } from '@/api/types'

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

export function getConversation(id: string): Promise<Conversation> {
  return request<Conversation>(`/conversations/${id}`)
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
