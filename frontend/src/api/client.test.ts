/**
 * Client behaviour that the app's security depends on:
 * the token never touching storage, one refresh for many 401s, and no loops.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  __resetClientForTests,
  getAccessToken,
  refreshAccessToken,
  request,
  setAccessToken,
} from '@/api/client'
import { ApiError, NetworkError } from '@/api/errors'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function unauthorized(): Response {
  return jsonResponse(401, {
    error: { code: 'authentication_required', message: 'Authentication is required.' },
  })
}

const tokenPayload = {
  access_token: 'fresh-token',
  token_type: 'bearer',
  expires_in: 900,
  user: {
    id: 'u1',
    email: 'a@b.com',
    display_name: 'A',
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
  },
}

type FetchMock = (input: string, init?: RequestInit) => Promise<Response>

let fetchMock: ReturnType<typeof vi.fn<FetchMock>>

beforeEach(() => {
  __resetClientForTests()
  fetchMock = vi.fn<FetchMock>()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('token storage', () => {
  it('keeps the access token in memory and never in browser storage', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { ok: true }))
    setAccessToken('secret-token')

    await request('/users/me')

    expect(getAccessToken()).toBe('secret-token')

    // Nothing anywhere in browser storage may contain the token.
    const readAll = (storage: Storage) =>
      Array.from({ length: storage.length }, (_, i) => storage.getItem(storage.key(i) ?? '') ?? '')
    /* eslint-disable no-restricted-properties -- this test exists to prove
       nothing lands in storage, so it has to look. */
    const stored = [...readAll(window.localStorage), ...readAll(window.sessionStorage)].join(' ')
    /* eslint-enable no-restricted-properties */
    expect(stored).not.toContain('secret-token')
  })

  it('sends the token as a bearer header', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}))
    setAccessToken('secret-token')

    await request('/users/me')

    const headers = fetchMock.mock.calls[0]?.[1]?.headers
    expect(headers).toBeInstanceOf(Headers)
    expect((headers as Headers).get('Authorization')).toBe('Bearer secret-token')
  })
})

describe('single-flight refresh', () => {
  it('issues exactly one refresh for many concurrent 401s', async () => {
    // Refresh rotation is single-use: a second concurrent refresh would
    // present an already-retired token and revoke the whole family.
    setAccessToken('stale-token')
    fetchMock.mockImplementation((input) => {
      if (input.endsWith('/auth/refresh')) {
        return new Promise<Response>((resolve) =>
          setTimeout(() => resolve(jsonResponse(200, tokenPayload)), 10),
        )
      }
      return Promise.resolve(
        getAccessToken() === 'fresh-token' ? jsonResponse(200, { ok: true }) : unauthorized(),
      )
    })

    const results = await Promise.all([
      request('/a'),
      request('/b'),
      request('/c'),
      request('/d'),
      request('/e'),
    ])

    const refreshCalls = fetchMock.mock.calls.filter((call) =>
      String(call[0]).endsWith('/auth/refresh'),
    )
    expect(refreshCalls).toHaveLength(1)
    expect(results).toHaveLength(5)
    expect(getAccessToken()).toBe('fresh-token')
  })

  it('starts a new refresh once the previous one has settled', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, tokenPayload))

    await refreshAccessToken()
    await refreshAccessToken()

    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})

describe('no retry loops', () => {
  it('surfaces the original 401 when the refresh fails, without retrying', async () => {
    setAccessToken('stale-token')
    fetchMock.mockImplementation(() => Promise.resolve(unauthorized()))

    await expect(request('/users/me')).rejects.toBeInstanceOf(ApiError)

    // Exactly two calls: the original request and one refresh attempt. The
    // request is never replayed, because refreshing produced no token.
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(getAccessToken()).toBeNull()
  })

  it('replays the request at most once', async () => {
    setAccessToken('stale-token')
    fetchMock.mockImplementation((input) =>
      Promise.resolve(
        input.endsWith('/auth/refresh') ? jsonResponse(200, tokenPayload) : unauthorized(),
      ),
    )

    await expect(request('/users/me')).rejects.toBeInstanceOf(ApiError)

    // original + refresh + one replay, then it gives up.
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('does not attempt a refresh when the caller opts out', async () => {
    fetchMock.mockResolvedValue(unauthorized())

    await expect(request('/auth/login', { retryOnUnauthorized: false })).rejects.toBeInstanceOf(
      ApiError,
    )

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe('error mapping', () => {
  it('exposes the server envelope', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(409, {
        error: {
          code: 'email_already_registered',
          message: 'An account with that email already exists.',
          request_id: 'req-1',
          details: null,
        },
      }),
    )

    const error = await request('/auth/register', { retryOnUnauthorized: false }).catch(
      (e: unknown) => e,
    )

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).is('email_already_registered')).toBe(true)
    expect((error as ApiError).requestId).toBe('req-1')
  })

  it('extracts field errors from a 422', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, {
        error: {
          code: 'validation_failed',
          message: 'The request body failed validation.',
          request_id: null,
          details: { fields: [{ field: 'email', reason: 'value is not a valid email address' }] },
        },
      }),
    )

    const error = (await request('/auth/register', { retryOnUnauthorized: false }).catch(
      (e: unknown) => e,
    )) as ApiError

    expect(error.fieldErrors()).toEqual({ email: 'value is not a valid email address' })
  })

  it('survives a non-JSON error body', async () => {
    fetchMock.mockResolvedValue(new Response('<html>502 Bad Gateway</html>', { status: 502 }))

    const error = (await request('/users/me').catch((e: unknown) => e)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(502)
  })

  it('reports a transport failure as a NetworkError', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    await expect(request('/users/me')).rejects.toBeInstanceOf(NetworkError)
  })
})
