/**
 * Email confirmation: the emailed link's page, and the prompt in the chat.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { App } from '@/app/App'
import {
  errorResponse,
  jsonResponse,
  readBody,
  renderApp,
  stubApi,
  testUser,
  tokenPayload,
} from '@/test/utils'

const unverified = { ...testUser, is_verified: false }
const anonymous = { '/auth/refresh': () => errorResponse(401, 'invalid_token') }

function signedInAs(user: typeof testUser) {
  return {
    '/auth/refresh': () => jsonResponse(200, { ...tokenPayload, user }),
    '/users/me': () => jsonResponse(200, user),
    'GET /conversations': () =>
      jsonResponse(200, { items: [], next_cursor: null, has_more: false }),
  }
}

describe('the confirmation link', () => {
  it('confirms the address once, even under StrictMode', async () => {
    const fetchMock = stubApi({
      ...anonymous,
      'POST /auth/verify-email': () => jsonResponse(200, { detail: 'Email confirmed.' }),
    })
    renderApp(<App />, { route: '/verify-email?token=signed-token-value' })

    expect(await screen.findByRole('heading', { name: 'Email confirmed' })).toBeInTheDocument()
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).includes('verify-email'))
    expect(calls).toHaveLength(1)
    expect(readBody(calls[0]?.[1])).toEqual({ token: 'signed-token-value' })
    // Signed out in this browser: the way on is to sign in.
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/login')
  })

  it('updates a signed-in session so the chat stops asking', async () => {
    let verified = false
    stubApi({
      ...signedInAs(unverified),
      '/users/me': () => jsonResponse(200, { ...testUser, is_verified: verified }),
      'POST /auth/verify-email': () => {
        verified = true
        return jsonResponse(200, { detail: 'Email confirmed.' })
      },
    })
    renderApp(<App />, { route: '/verify-email?token=signed-token-value' })

    await screen.findByRole('heading', { name: 'Email confirmed' })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('link', { name: 'Start chatting' }))

    await screen.findAllByRole('button', { name: 'New chat' })
    await waitFor(() =>
      expect(screen.queryByText(/confirm your email to start chatting/i)).not.toBeInTheDocument(),
    )
  })

  it('explains an expired or invalid link', async () => {
    stubApi({
      ...anonymous,
      'POST /auth/verify-email': () => errorResponse(401, 'invalid_token'),
    })
    renderApp(<App />, { route: '/verify-email?token=signed-token-value' })

    expect(await screen.findByRole('alert')).toHaveTextContent(/expired or is not valid/i)
  })

  it('says so when the link has no token', async () => {
    const fetchMock = stubApi(anonymous)
    renderApp(<App />, { route: '/verify-email' })

    expect(await screen.findByRole('alert')).toHaveTextContent(/missing its token/i)
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('verify-email'))).toBe(false)
  })
})

describe('the prompt in the chat', () => {
  it('asks an unverified account to confirm, and resends on request', async () => {
    let resent = 0
    stubApi({
      ...signedInAs(unverified),
      'POST /auth/resend-verification': () => {
        resent += 1
        return jsonResponse(202, { detail: 'Verification email sent.' })
      },
    })
    renderApp(<App />, { route: '/chat' })

    expect(await screen.findByText(/confirm your email to start chatting/i)).toHaveTextContent(
      testUser.email,
    )
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Resend email' }))

    expect(await screen.findByText(/check your inbox for a new link/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Email sent' })).toBeDisabled()
    expect(resent).toBe(1)
  })

  it('explains a resend refused by the rate limit', async () => {
    stubApi({
      ...signedInAs(unverified),
      'POST /auth/resend-verification': () => errorResponse(429, 'rate_limited'),
    })
    renderApp(<App />, { route: '/chat' })

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Resend email' }))

    expect(await screen.findByText(/too many emails requested/i)).toBeInTheDocument()
  })

  it('is not shown to a verified account', async () => {
    stubApi(signedInAs(testUser))
    renderApp(<App />, { route: '/chat' })

    await screen.findAllByRole('button', { name: 'New chat' })
    expect(screen.queryByText(/confirm your email/i)).not.toBeInTheDocument()
  })
})
