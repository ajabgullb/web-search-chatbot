/**
 * AI provider settings: the user's own LLM API keys (ADR-0020).
 *
 * A key goes to the server once, in a save, and never comes back: the server
 * stores it encrypted and returns only its last four characters. Every change
 * answers with the whole settings object, so the picker updates at once.
 */

import { request } from '@/api/client'
import type { LlmCredentialWrite, LlmSelectionWrite, LlmSettings } from '@/api/types'

export function getLlmSettings(): Promise<LlmSettings> {
  return request<LlmSettings>('/users/me/llm-settings')
}

/** Check the key with the provider, save it, and switch to it. */
export function saveLlmKey(provider: string, body: LlmCredentialWrite): Promise<LlmSettings> {
  return request<LlmSettings>(`/users/me/llm-credentials/${encodeURIComponent(provider)}`, {
    method: 'PUT',
    body,
  })
}

export function deleteLlmKey(provider: string): Promise<void> {
  return request<void>(`/users/me/llm-credentials/${encodeURIComponent(provider)}`, {
    method: 'DELETE',
  })
}

/** Switch models; both null picks the app's default. Instant on the server. */
export function selectLlm(body: LlmSelectionWrite): Promise<LlmSettings> {
  return request<LlmSettings>('/users/me/llm-selection', { method: 'PUT', body })
}
