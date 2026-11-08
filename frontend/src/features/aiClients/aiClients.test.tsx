/**
 * The AI clients page, against a stubbed API.
 *
 * What a user would notice if it broke: the page is reachable from the
 * sidebar, a new token is shown once with the configuration it goes in, a
 * token needs a name and a permission, and revoking asks first.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { App } from '@/app/App'
import { claudeCodeCommand, jsonConfig } from '@/features/aiClients/connection'
import {
  errorResponse,
  jsonResponse,
  readBody,
  renderApp,
  stubApi,
  testUser,
  tokenPayload,
} from '@/test/utils'

const TOKEN_ID = '44444444-4444-4444-8444-444444444444'
const SECRET = 'wsc_pat_abcdEFGHijklMNOPqrstUVWXyz0123456789-_ABCDE'

function token(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: TOKEN_ID,
    name: 'Claude Code',
    token_prefix: 'wsc_pat_abcd',
    scopes: ['conversations:read', 'runs:create'],
    expires_at: '2099-01-01T00:00:00Z',
    last_used_at: null,
    created_at: '2026-11-07T10:00:00Z',
    ...overrides,
  }
}

function routes(
  tokens: () => unknown[],
  extra: Record<string, Parameters<typeof stubApi>[0][string]> = {},
) {
  return {
    '/auth/refresh': () => jsonResponse(200, tokenPayload),
    '/users/me': () => jsonResponse(200, testUser),
    'GET /conversations': () =>
      jsonResponse(200, { items: [], next_cursor: null, has_more: false }),
    'GET /access-tokens': () => jsonResponse(200, { items: tokens() }),
    ...extra,
  }
}

describe('AI clients page', () => {
  it('is reachable from the sidebar', async () => {
    stubApi(routes(() => []))
    renderApp(<App />, { route: '/chat' })

    await userEvent.click(await screen.findByRole('link', { name: 'AI clients' }))

    expect(await screen.findByRole('heading', { name: 'AI clients' })).toBeInTheDocument()
    expect(await screen.findByText('No tokens yet.')).toBeInTheDocument()
  })

  it('shows a new token once, with the configuration it goes in', async () => {
    const user = userEvent.setup()
    let stored: unknown[] = []
    const fetchMock = stubApi(
      routes(() => stored, {
        'POST /access-tokens': ({ init }) => {
          const body = readBody<{ name: string; scopes: string[]; expires_in_days: number }>(init)
          stored = [token({ name: body.name, scopes: body.scopes })]
          return jsonResponse(201, {
            ...token({ name: body.name, scopes: body.scopes }),
            token: SECRET,
          })
        },
      }),
    )
    renderApp(<App />, { route: '/settings/ai-clients' })

    await user.type(await screen.findByLabelText('Name'), 'Claude Code')
    await user.click(screen.getByLabelText(/Ask questions/))
    await user.selectOptions(screen.getByLabelText('Expires after'), '7')
    await user.click(screen.getByRole('button', { name: 'Create token' }))

    expect(await screen.findByRole('heading', { name: 'Connect Claude Code' })).toBeInTheDocument()
    const posted = fetchMock.mock.calls.find(
      ([input, init]) => init?.method === 'POST' && String(input).includes('/access-tokens'),
    )
    expect(readBody(posted?.[1])).toEqual({
      name: 'Claude Code',
      scopes: ['conversations:read'],
      expires_in_days: 7,
    })
    const url = `${window.location.origin}/api/v1/mcp`
    expect(screen.getByText(SECRET)).toBeInTheDocument()
    expect(screen.getByText(url)).toBeInTheDocument()
    expect(screen.getByText(claudeCodeCommand(url, SECRET))).toBeInTheDocument()
    expect(
      screen.getByText(
        (_, node) => node?.tagName === 'CODE' && node.textContent === jsonConfig(url, SECRET),
      ),
    ).toBeInTheDocument()
    // The list refreshed, and shows only the prefix.
    const list = screen.getByRole('list')
    expect(await within(list).findByText('wsc_pat_abcd…')).toBeInTheDocument()
    expect(within(list).queryByText(SECRET)).not.toBeInTheDocument()

    // Done hides the token for good.
    await user.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Create a token' })).toBeInTheDocument()
  })

  it('needs a name and at least one permission', async () => {
    const user = userEvent.setup()
    const fetchMock = stubApi(routes(() => []))
    renderApp(<App />, { route: '/settings/ai-clients' })

    await user.click(await screen.findByRole('button', { name: 'Create token' }))
    expect(screen.getByText(/Give the token a name/)).toBeInTheDocument()

    await user.click(screen.getByLabelText(/Read your conversations/))
    await user.click(screen.getByLabelText(/Ask questions/))
    expect(screen.getByRole('button', { name: 'Create token' })).toBeDisabled()
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) => init?.method === 'POST' && String(input).includes('/access-tokens'),
      ),
    ).toBe(false)
  })

  it('explains a refusal from the server', async () => {
    const user = userEvent.setup()
    stubApi(
      routes(() => [], {
        'POST /access-tokens': () =>
          errorResponse(
            409,
            'access_token_limit_reached',
            'An account can have at most 10 access tokens.',
          ),
      }),
    )
    renderApp(<App />, { route: '/settings/ai-clients' })

    await user.type(await screen.findByLabelText('Name'), 'One too many')
    await user.click(screen.getByRole('button', { name: 'Create token' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('at most 10 access tokens')
  })

  it('asks before revoking, then removes the token', async () => {
    const user = userEvent.setup()
    let stored: unknown[] = [token({ last_used_at: '2026-11-07T11:00:00Z' })]
    const fetchMock = stubApi(
      routes(() => stored, {
        [`DELETE /access-tokens/${TOKEN_ID}`]: () => {
          stored = []
          return new Response(null, { status: 204 })
        },
      }),
    )
    renderApp(<App />, { route: '/settings/ai-clients' })

    await user.click(await screen.findByRole('button', { name: 'Revoke Claude Code' }))
    expect(screen.getByText(/stop working at once/)).toBeInTheDocument()
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) => init?.method === 'DELETE' && String(input).includes('/access-tokens'),
      ),
    ).toBe(false)

    await user.click(screen.getByRole('button', { name: 'Confirm revoke Claude Code' }))

    await waitFor(() => expect(screen.getByText('No tokens yet.')).toBeInTheDocument())
  })

  it('marks an expired token', async () => {
    stubApi(routes(() => [token({ expires_at: '2020-01-01T00:00:00Z' })]))
    renderApp(<App />, { route: '/settings/ai-clients' })

    expect(await screen.findByText('Expired')).toBeInTheDocument()
    expect(screen.getByText(/Never used/)).toBeInTheDocument()
  })
})
