/** Helpers shared by component tests. */

import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { vi } from 'vitest'

import { __resetClientForTests } from '@/api/client'

export const testUser = {
  id: '11111111-1111-4111-8111-111111111111',
  email: 'user@example.com',
  display_name: 'Test User',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
}

export const tokenPayload = {
  access_token: 'access-token',
  token_type: 'bearer' as const,
  expires_in: 900,
  user: testUser,
}

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

export function errorResponse(status: number, code: string, message = 'Failed.'): Response {
  return jsonResponse(status, { error: { code, message, request_id: null, details: null } })
}

/**
 * Install a fetch stub that routes by path.
 *
 * Unmatched paths return 401, which is what an unauthenticated API does — so a
 * test that forgets to stub an endpoint fails loudly rather than hanging.
 */
type FetchMock = (input: string, init?: RequestInit) => Promise<Response>

export function stubApi(routes: Record<string, () => Response | Promise<Response>>) {
  const fetchMock = vi.fn<FetchMock>((input) => {
    const path = String(input).replace('/api/v1', '')
    const handler = routes[path]
    if (handler) return Promise.resolve(handler())
    return Promise.resolve(errorResponse(401, 'authentication_required'))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

export function renderApp(ui: ReactElement, { route = '/' } = {}) {
  __resetClientForTests()
  window.history.pushState({}, '', route)
  return render(ui)
}
