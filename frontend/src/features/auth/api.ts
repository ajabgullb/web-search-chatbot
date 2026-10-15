/**
 * Auth endpoint wrappers.
 *
 * Each one is a thin, typed call. None of them touches component state — the
 * context owns that, so these stay usable from tests and future callers.
 */

import { request, setAccessToken } from '@/api/client'
import type {
  ForgotPasswordRequest,
  LoginRequest,
  MessageResponse,
  RegisterRequest,
  ResetPasswordRequest,
  TokenResponse,
  User,
  VerifyEmailRequest,
} from '@/api/types'

export async function register(payload: RegisterRequest): Promise<TokenResponse> {
  const tokens = await request<TokenResponse>('/auth/register', {
    method: 'POST',
    body: payload,
    // A 401 here means the credentials were wrong, not that a session expired.
    retryOnUnauthorized: false,
  })
  setAccessToken(tokens.access_token)
  return tokens
}

export async function login(payload: LoginRequest): Promise<TokenResponse> {
  const tokens = await request<TokenResponse>('/auth/login', {
    method: 'POST',
    body: payload,
    retryOnUnauthorized: false,
  })
  setAccessToken(tokens.access_token)
  return tokens
}

export function logout(): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/logout', {
    method: 'POST',
    retryOnUnauthorized: false,
  })
}

export function forgotPassword(payload: ForgotPasswordRequest): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/forgot-password', {
    method: 'POST',
    body: payload,
    retryOnUnauthorized: false,
  })
}

export function resetPassword(payload: ResetPasswordRequest): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/reset-password', {
    method: 'POST',
    body: payload,
    retryOnUnauthorized: false,
  })
}

/** Unauthenticated: the emailed link may be opened in a signed-out browser. */
export function verifyEmail(payload: VerifyEmailRequest): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/verify-email', {
    method: 'POST',
    body: payload,
    retryOnUnauthorized: false,
  })
}

export function resendVerification(): Promise<MessageResponse> {
  return request<MessageResponse>('/auth/resend-verification', { method: 'POST' })
}

export function fetchCurrentUser(): Promise<User> {
  return request<User>('/users/me')
}
