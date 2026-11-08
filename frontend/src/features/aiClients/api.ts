/**
 * Personal access token endpoints (ADR-0019).
 *
 * Called with the browser's ordinary session. The token a create returns is
 * shown once and never fetched again: the server keeps only its hash.
 */

import { request } from '@/api/client'
import type { AccessTokenCreate, AccessTokenCreated, AccessTokenList } from '@/api/types'

export function listAccessTokens(): Promise<AccessTokenList> {
  return request<AccessTokenList>('/access-tokens')
}

export function createAccessToken(body: AccessTokenCreate): Promise<AccessTokenCreated> {
  return request<AccessTokenCreated>('/access-tokens', { method: 'POST', body })
}

export function revokeAccessToken(id: string): Promise<void> {
  return request<void>(`/access-tokens/${id}`, { method: 'DELETE' })
}
