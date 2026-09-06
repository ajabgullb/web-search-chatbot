/** Helpers shared by component tests. */

import { render } from '@testing-library/react'
import { StrictMode } from 'react'
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
 * Keys may be a bare path (`/users/me`), a path with a query string, or either
 * prefixed by a method (`POST /conversations`). More specific keys win, so a
 * test can stub `GET /conversations` and `POST /conversations` separately.
 *
 * Unmatched paths return 401, which is what an unauthenticated API does — so a
 * test that forgets to stub an endpoint fails loudly rather than hanging.
 */
type FetchMock = (input: string, init?: RequestInit) => Promise<Response>
type StubRequest = { url: URL; init: RequestInit | undefined }
type RouteHandler = (request: StubRequest) => Response | Promise<Response>

/**
 * Read a stubbed request's JSON body.
 *
 * `fetch` types `body` as a broad BodyInit; the client only ever sends a JSON
 * string, so the narrowing is safe and is done once here rather than at every
 * call site.
 */
export function readBody<T>(init: RequestInit | undefined): T {
  const body = init?.body
  // Narrow rather than coerce: BodyInit also covers Blob and FormData, and
  // stringifying one of those would silently parse "[object Blob]".
  return JSON.parse(typeof body === 'string' ? body : '{}') as T
}

export function stubApi(routes: Record<string, RouteHandler>) {
  const fetchMock = vi.fn<FetchMock>((input, init) => {
    const method = (init?.method ?? 'GET').toUpperCase()
    // A base is required because the app calls fetch with a relative path.
    const url = new URL(String(input), 'http://localhost')
    const path = url.pathname.replace('/api/v1', '')

    for (const key of [
      `${method} ${path}${url.search}`,
      `${method} ${path}`,
      `${path}${url.search}`,
      path,
    ]) {
      const handler = routes[key]
      if (handler) return Promise.resolve(handler({ url, init }))
    }
    return Promise.resolve(errorResponse(401, 'authentication_required'))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/**
 * Render under `StrictMode`, exactly as `main.tsx` does.
 *
 * Not a detail: StrictMode mounts effects twice, and an effect whose cleanup
 * cancels shared in-flight work will hang forever in the browser while passing
 * a plain `render`. Tests must exercise the same path production runs.
 */
export function renderApp(ui: ReactElement, { route = '/' } = {}) {
  __resetClientForTests()
  window.history.pushState({}, '', route)
  return render(<StrictMode>{ui}</StrictMode>)
}
