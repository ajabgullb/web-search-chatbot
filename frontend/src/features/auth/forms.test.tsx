/**
 * Form behaviour: validation, server-error surfacing, and accessibility wiring.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { App } from '@/app/App'
import { errorResponse, jsonResponse, renderApp, stubApi, tokenPayload } from '@/test/utils'

const anonymous = { '/auth/refresh': () => errorResponse(401, 'invalid_token') }

describe('sign-in form', () => {
  it('validates before making a request', async () => {
    const user = userEvent.setup()
    const fetchMock = stubApi(anonymous)
    renderApp(<App />, { route: '/login' })

    await screen.findByRole('heading', { name: /sign in/i })
    await user.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByText(/enter your email address/i)).toBeInTheDocument()
    expect(await screen.findByText(/enter a password/i)).toBeInTheDocument()
    // Nothing beyond the bootstrap refresh: invalid input never hits the API.
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('marks invalid fields for assistive technology', async () => {
    const user = userEvent.setup()
    stubApi(anonymous)
    renderApp(<App />, { route: '/login' })

    await screen.findByRole('heading', { name: /sign in/i })
    await user.click(screen.getByRole('button', { name: /sign in/i }))

    const email = await screen.findByLabelText(/email/i)
    // Colour alone would fail WCAG 1.4.1, so the state is in the markup too.
    expect(email).toHaveAttribute('aria-invalid', 'true')
    expect(email).toHaveAccessibleDescription(/enter your email address/i)
  })

  it('shows the server message when credentials are wrong', async () => {
    const user = userEvent.setup()
    stubApi({
      ...anonymous,
      '/auth/login': () =>
        errorResponse(401, 'invalid_credentials', 'Email or password is incorrect.'),
    })
    renderApp(<App />, { route: '/login' })

    await screen.findByRole('heading', { name: /sign in/i })
    await user.type(screen.getByLabelText(/email/i), 'user@example.com')
    await user.type(screen.getByLabelText(/password/i), 'wrong-password-entirely')
    await user.click(screen.getByRole('button', { name: /sign in/i }))

    // role="alert" means it is announced, not merely displayed.
    expect(await screen.findByRole('alert')).toHaveTextContent(/email or password is incorrect/i)
  })

  it('explains a rate limit in its own words', async () => {
    const user = userEvent.setup()
    stubApi({ ...anonymous, '/auth/login': () => errorResponse(429, 'rate_limited') })
    renderApp(<App />, { route: '/login' })

    await screen.findByRole('heading', { name: /sign in/i })
    await user.type(screen.getByLabelText(/email/i), 'user@example.com')
    await user.type(screen.getByLabelText(/password/i), 'correct-horse-battery-staple')
    await user.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/too many attempts/i)
  })
})

describe('registration form', () => {
  it('requires a password of at least 12 characters', async () => {
    const user = userEvent.setup()
    const fetchMock = stubApi(anonymous)
    renderApp(<App />, { route: '/register' })

    await screen.findByRole('heading', { name: /create an account/i })
    await user.type(screen.getByLabelText(/display name/i), 'Test User')
    await user.type(screen.getByLabelText(/email/i), 'user@example.com')
    await user.type(screen.getByLabelText(/^password$/i), 'short')
    await user.click(screen.getByRole('button', { name: /create account/i }))

    // The field's hint also mentions 12 characters, so assert on the error's
    // own wording rather than a substring both share.
    expect(await screen.findByText(/use at least 12 characters/i)).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('reports a duplicate email against the email field', async () => {
    const user = userEvent.setup()
    stubApi({
      ...anonymous,
      '/auth/register': () => errorResponse(409, 'email_already_registered'),
    })
    renderApp(<App />, { route: '/register' })

    await screen.findByRole('heading', { name: /create an account/i })
    await user.type(screen.getByLabelText(/display name/i), 'Test User')
    await user.type(screen.getByLabelText(/email/i), 'taken@example.com')
    await user.type(screen.getByLabelText(/^password$/i), 'correct-horse-battery-staple')
    await user.click(screen.getByRole('button', { name: /create account/i }))

    expect(await screen.findByLabelText(/email/i)).toHaveAccessibleDescription(/already exists/i)
  })

  it('surfaces server field errors from a 422', async () => {
    const user = userEvent.setup()
    stubApi({
      ...anonymous,
      '/auth/register': () =>
        jsonResponse(422, {
          error: {
            code: 'validation_failed',
            message: 'The request body failed validation.',
            request_id: null,
            details: { fields: [{ field: 'email', reason: 'not a valid email address' }] },
          },
        }),
    })
    renderApp(<App />, { route: '/register' })

    await screen.findByRole('heading', { name: /create an account/i })
    await user.type(screen.getByLabelText(/display name/i), 'Test User')
    await user.type(screen.getByLabelText(/email/i), 'user@example.com')
    await user.type(screen.getByLabelText(/^password$/i), 'correct-horse-battery-staple')
    await user.click(screen.getByRole('button', { name: /create account/i }))

    expect(await screen.findByText(/not a valid email address/i)).toBeInTheDocument()
  })
})

describe('forgot password', () => {
  it('confirms without revealing whether the account exists', async () => {
    const user = userEvent.setup()
    stubApi({
      ...anonymous,
      '/auth/forgot-password': () => jsonResponse(202, { detail: 'If an account exists…' }),
    })
    renderApp(<App />, { route: '/forgot-password' })

    await screen.findByRole('heading', { name: /reset your password/i })
    await user.type(screen.getByLabelText(/email/i), 'ghost@example.com')
    await user.click(screen.getByRole('button', { name: /send reset link/i }))

    // Mirrors the server, which answers identically for a real and an
    // unknown address.
    expect(await screen.findByRole('alert')).toHaveTextContent(/if an account exists/i)
  })
})

describe('reset password', () => {
  it('refuses a link with no token', async () => {
    stubApi(anonymous)
    renderApp(<App />, { route: '/reset-password' })

    expect(
      await screen.findByRole('heading', { name: /reset link is incomplete/i }),
    ).toBeInTheDocument()
  })

  it('requires the confirmation to match', async () => {
    const user = userEvent.setup()
    stubApi(anonymous)
    renderApp(<App />, { route: '/reset-password?token=abc123' })

    await screen.findByRole('heading', { name: /choose a new password/i })
    await user.type(screen.getByLabelText(/^new password$/i), 'an-entirely-new-passphrase')
    await user.type(screen.getByLabelText(/confirm new password/i), 'something-else-entirely')
    await user.click(screen.getByRole('button', { name: /save new password/i }))

    expect(await screen.findByText(/passwords do not match/i)).toBeInTheDocument()
  })

  it('sends the user to sign in after a successful reset', async () => {
    const user = userEvent.setup()
    stubApi({
      ...anonymous,
      '/auth/reset-password': () => jsonResponse(200, { detail: 'Password updated.' }),
    })
    renderApp(<App />, { route: '/reset-password?token=abc123' })

    await screen.findByRole('heading', { name: /choose a new password/i })
    await user.type(screen.getByLabelText(/^new password$/i), 'an-entirely-new-passphrase')
    await user.type(screen.getByLabelText(/confirm new password/i), 'an-entirely-new-passphrase')
    await user.click(screen.getByRole('button', { name: /save new password/i }))

    await waitFor(() => expect(window.location.pathname).toBe('/login'))
  })

  it('explains an expired or reused link', async () => {
    const user = userEvent.setup()
    stubApi({ ...anonymous, '/auth/reset-password': () => errorResponse(401, 'invalid_token') })
    renderApp(<App />, { route: '/reset-password?token=abc123' })

    await screen.findByRole('heading', { name: /choose a new password/i })
    await user.type(screen.getByLabelText(/^new password$/i), 'an-entirely-new-passphrase')
    await user.type(screen.getByLabelText(/confirm new password/i), 'an-entirely-new-passphrase')
    await user.click(screen.getByRole('button', { name: /save new password/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/expired or has already been used/i)
  })
})

describe('sign out', () => {
  it('ends the session even if the server call fails', async () => {
    const user = userEvent.setup()
    stubApi({
      '/auth/refresh': () => jsonResponse(200, tokenPayload),
      '/users/me': () => jsonResponse(200, tokenPayload.user),
      '/auth/logout': () => errorResponse(500, 'internal_error'),
    })
    renderApp(<App />, { route: '/chat' })

    await screen.findByText(/signed in as/i)
    await user.click(screen.getByRole('button', { name: /sign out/i }))

    // A failed logout must never strand someone signed in locally.
    await waitFor(() => expect(window.location.pathname).toBe('/login'))
  })
})
