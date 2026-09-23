/**
 * The API client, and the only place that knows how the access token is held.
 *
 * Three rules drive the design:
 *
 * 1. **The access token lives in a module variable, never in browser storage.**
 *    Anything readable by script is readable by injected script; `localStorage`
 *    also survives the tab, so a shared machine keeps the session. The cost is
 *    that a page reload loses it — which is exactly what `bootstrap()` and the
 *    refresh cookie are for.
 * 2. **Concurrent 401s trigger exactly one refresh.** Five queries failing at
 *    once must not fire five rotations: refresh rotation is single-use, so the
 *    second one would present an already-retired token and the server would
 *    revoke the whole family. See `refreshAccessToken`.
 * 3. **A request is retried at most once.** If the replay 401s too, the caller
 *    gets the error. There is no path here that can loop.
 */

import { ApiError, NetworkError } from '@/api/errors'
import type { ErrorBody, TokenResponse } from '@/api/types'

const API_PREFIX = '/api/v1'

/** In memory for the lifetime of the page, and nowhere else. */
let accessToken: string | null = null

/** Notified whenever the token is set or cleared, so React can re-render. */
type SessionListener = (token: string | null) => void
const listeners = new Set<SessionListener>()

export function getAccessToken(): string | null {
  return accessToken
}

export function setAccessToken(token: string | null): void {
  accessToken = token
  for (const listener of listeners) listener(token)
}

export function onSessionChange(listener: SessionListener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/**
 * The in-flight refresh, if any.
 *
 * Every caller that needs a refresh awaits this same promise rather than
 * starting its own — the "single-flight" pattern. It is cleared in a `finally`
 * so a later failure does not permanently poison the client.
 */
let refreshInFlight: Promise<string | null> | null = null

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  /** Set false on auth endpoints that must not trigger a refresh. */
  retryOnUnauthorized?: boolean
  signal?: AbortSignal
  /** Extra request headers, such as `Idempotency-Key`. */
  headers?: Record<string, string>
  accept?: string
}

export async function readError(response: Response): Promise<ApiError> {
  let body: ErrorBody = {
    code: `http_${response.status}`,
    message: 'The request failed.',
    request_id: response.headers.get('X-Request-ID'),
    details: null,
  }
  try {
    const parsed: unknown = await response.json()
    if (
      typeof parsed === 'object' &&
      parsed !== null &&
      'error' in parsed &&
      typeof parsed.error === 'object' &&
      parsed.error !== null
    ) {
      body = parsed.error as ErrorBody
    }
  } catch {
    // A non-JSON error body (a proxy's 502 page, say) keeps the default above.
  }
  return new ApiError(response.status, body)
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers = new Headers({ ...options.headers, Accept: options.accept ?? 'application/json' })
  if (options.body !== undefined) headers.set('Content-Type', 'application/json')
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)

  try {
    return await fetch(`${API_PREFIX}${path}`, {
      method: options.method ?? 'GET',
      headers,
      // Sends the HttpOnly refresh cookie on the /auth routes it is scoped to.
      credentials: 'include',
      // Spread rather than assign undefined: `exactOptionalPropertyTypes`
      // distinguishes "absent" from "present and undefined".
      ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
      ...(options.signal ? { signal: options.signal } : {}),
    })
  } catch (cause) {
    // fetch only rejects on transport failure; HTTP errors resolve normally.
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new NetworkError(cause)
  }
}

/**
 * Rotate the refresh cookie for a new access token, at most once at a time.
 *
 * Returns the new token, or null when the session is genuinely over. Callers
 * must not treat null as an error worth retrying.
 */
export function refreshAccessToken(): Promise<string | null> {
  refreshInFlight ??= (async () => {
    try {
      const response = await send('/auth/refresh', {
        method: 'POST',
        retryOnUnauthorized: false,
      })
      if (!response.ok) {
        setAccessToken(null)
        return null
      }
      const tokens = (await response.json()) as TokenResponse
      setAccessToken(tokens.access_token)
      return tokens.access_token
    } catch {
      // A network blip is not proof the session ended, but there is no token
      // to return either. The caller surfaces the original failure.
      return null
    } finally {
      refreshInFlight = null
    }
  })()

  return refreshInFlight
}

/** Send, and on a 401 refresh once and replay once. Never more. */
async function sendWithRefresh(path: string, options: RequestOptions): Promise<Response> {
  let response = await send(path, options)

  if (response.status === 401 && (options.retryOnUnauthorized ?? true)) {
    const token = await refreshAccessToken()
    // Only replay if refreshing actually produced a token; otherwise fall
    // through and report the original 401. This is what keeps a dead session
    // from becoming a retry loop.
    if (token !== null) {
      response = await send(path, options)
    }
  }
  return response
}

/**
 * Perform an API request, transparently refreshing once on a 401.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await sendWithRefresh(path, options)

  if (!response.ok) throw await readError(response)
  if (response.status === 204) return undefined as T

  return (await response.json()) as T
}

/**
 * Open a streaming response, under the same refresh-once rule as `request`.
 *
 * Returns the `Response` with its body unread; the caller consumes the
 * stream. Errors before the stream starts (validation, 404, 409, 429) arrive
 * as ordinary JSON envelopes and are thrown as `ApiError`, exactly as for any
 * other call. Replaying after a refresh is safe for runs because the replay
 * carries the same `Idempotency-Key`.
 */
export async function openStream(path: string, options: RequestOptions = {}): Promise<Response> {
  const response = await sendWithRefresh(path, { accept: 'text/event-stream', ...options })
  if (!response.ok) throw await readError(response)
  return response
}

/** Discard the in-memory session. Does not call the server. */
export function clearSession(): void {
  setAccessToken(null)
}

/** Test seam: reset module state between cases. */
export function __resetClientForTests(): void {
  accessToken = null
  refreshInFlight = null
  listeners.clear()
}
