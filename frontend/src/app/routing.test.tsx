/**
 * Route guards and the auth bootstrap.
 *
 * The behaviour worth protecting: a signed-in user survives a reload without
 * being bounced to /login, an anonymous user is sent there and returned to
 * where they were going, and a failed bootstrap settles instead of looping.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { App } from '@/app/App'
import {
  errorResponse,
  jsonResponse,
  renderApp,
  stubApi,
  testUser,
  tokenPayload,
} from '@/test/utils'

describe('public routes', () => {
  it('shows the landing page at the root', async () => {
    stubApi({})
    renderApp(<App />, { route: '/' })

    expect(
      await screen.findByRole('heading', { name: /answers you can check/i }),
    ).toBeInTheDocument()
  })

  it('shows a 404 page for an unknown route', async () => {
    stubApi({})
    renderApp(<App />, { route: '/nope' })

    expect(await screen.findByRole('heading', { name: /page not found/i })).toBeInTheDocument()
  })
})

describe('the auth bootstrap', () => {
  it('attempts exactly one refresh on load', async () => {
    const fetchMock = stubApi({
      '/auth/refresh': () => errorResponse(401, 'invalid_token'),
    })
    renderApp(<App />, { route: '/' })

    await screen.findByRole('heading', { name: /answers you can check/i })

    const refreshCalls = fetchMock.mock.calls.filter((call) =>
      String(call[0]).endsWith('/auth/refresh'),
    )
    expect(refreshCalls).toHaveLength(1)
  })

  it('restores a session from the refresh cookie', async () => {
    stubApi({
      '/auth/refresh': () => jsonResponse(200, tokenPayload),
      '/users/me': () => jsonResponse(200, testUser),
    })
    renderApp(<App />, { route: '/chat' })

    // A reload with a live cookie must land on /chat, not on /login. The
    // sidebar's account block is the proof the guarded shell actually rendered.
    expect(await screen.findByText('user@example.com')).toBeInTheDocument()
  })

  it('resolves under StrictMode, whose cleanup runs before the refresh returns', async () => {
    // Regression: the bootstrap used a `cancelled` flag set by the effect
    // cleanup. StrictMode mounts, cleans up, then remounts — so the only
    // in-flight attempt was discarded and the app sat on the spinner forever,
    // with /login and /register showing nothing but a loader. `renderApp`
    // wraps in StrictMode, so this test sees the same double-mount as the
    // browser does.
    stubApi({
      '/auth/refresh': () => jsonResponse(200, tokenPayload),
      '/users/me': () => jsonResponse(200, testUser),
    })
    renderApp(<App />, { route: '/login' })

    await waitFor(() =>
      expect(screen.queryByText(/restoring your session/i)).not.toBeInTheDocument(),
    )
  })

  it('settles as anonymous when the refresh fails, without looping', async () => {
    const fetchMock = stubApi({
      '/auth/refresh': () => errorResponse(401, 'invalid_token'),
    })
    renderApp(<App />, { route: '/chat' })

    expect(await screen.findByRole('heading', { name: /sign in/i })).toBeInTheDocument()

    // Give any runaway retry a chance to show itself.
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(2)
  })
})

describe('route guards', () => {
  it('redirects an anonymous visitor away from a guarded route', async () => {
    stubApi({ '/auth/refresh': () => errorResponse(401, 'invalid_token') })
    renderApp(<App />, { route: '/chat' })

    expect(await screen.findByRole('heading', { name: /sign in/i })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/login')
  })

  it('returns the user to the route they originally wanted', async () => {
    // A specific conversation, not /chat: /chat is where sign-in lands by
    // default, so it could not tell a restored destination from a lost one.
    const destination = '/chat/0b9e5c0e-6d7a-4f1e-9d8a-3c2b1a0f9e8d'
    const user = userEvent.setup()
    stubApi({
      '/auth/refresh': () => errorResponse(401, 'invalid_token'),
      '/auth/login': () => jsonResponse(200, tokenPayload),
    })
    renderApp(<App />, { route: destination })

    await screen.findByRole('heading', { name: /sign in/i })
    await user.type(screen.getByLabelText(/email/i), 'user@example.com')
    await user.type(screen.getByLabelText(/password/i), 'correct-horse-battery-staple')
    await user.click(screen.getByRole('button', { name: /sign in/i }))

    await waitFor(() => expect(window.location.pathname).toBe(destination))
  })

  it('keeps a signed-in user off the login page', async () => {
    stubApi({
      '/auth/refresh': () => jsonResponse(200, tokenPayload),
      '/users/me': () => jsonResponse(200, testUser),
    })
    renderApp(<App />, { route: '/login' })

    await waitFor(() => expect(window.location.pathname).toBe('/chat'))
  })

  it('lets an authenticated user complete a reset from an emailed link', async () => {
    // Public on purpose: a stale session in another tab must not block a reset.
    stubApi({
      '/auth/refresh': () => jsonResponse(200, tokenPayload),
      '/users/me': () => jsonResponse(200, testUser),
    })
    renderApp(<App />, { route: '/reset-password?token=abc123' })

    expect(
      await screen.findByRole('heading', { name: /choose a new password/i }),
    ).toBeInTheDocument()
  })
})
