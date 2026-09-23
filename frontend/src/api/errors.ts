import type { ErrorBody } from '@/api/types'

/** Stable error codes the UI branches on. Mirrors `app/api/errors.py`. */
export type ApiErrorCode =
  | 'validation_failed'
  | 'authentication_required'
  | 'invalid_credentials'
  | 'invalid_token'
  | 'account_inactive'
  | 'origin_not_allowed'
  | 'email_already_registered'
  | 'rate_limited'
  | 'conversation_not_found'
  | 'invalid_cursor'
  | 'internal_error'
  | 'run_not_found'
  | 'message_not_found'
  | 'run_in_progress'
  | 'retry_not_allowed'
  | 'idempotency_key_reused'
  | 'message_empty'
  | 'message_too_long'

/**
 * A failed API call, carrying the server's error envelope.
 *
 * The server's `message` is written for humans and is safe to show, so the UI
 * does not maintain a parallel copy of every message; it only overrides where
 * it can say something more useful in context.
 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string | null
  readonly details: Record<string, unknown> | null

  constructor(status: number, body: ErrorBody) {
    super(body.message)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code
    this.requestId = body.request_id ?? null
    this.details = (body.details as Record<string, unknown> | null) ?? null
  }

  is(code: ApiErrorCode): boolean {
    return this.code === code
  }

  /** Field-level failures from a 422, as `{ fieldName: reason }`. */
  fieldErrors(): Record<string, string> {
    const fields: unknown = this.details?.fields
    if (!Array.isArray(fields)) return {}

    const result: Record<string, string> = {}
    for (const entry of fields as unknown[]) {
      if (isFieldError(entry)) result[entry.field] = entry.reason
    }
    return result
  }
}

interface FieldError {
  field: string
  reason: string
}

function isFieldError(value: unknown): value is FieldError {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as Record<string, unknown>
  return typeof candidate.field === 'string' && typeof candidate.reason === 'string'
}

/** A transport failure: the request never produced an HTTP response. */
export class NetworkError extends Error {
  constructor(cause: unknown) {
    super('Could not reach the server. Check your connection and try again.')
    this.name = 'NetworkError'
    this.cause = cause
  }
}
