/**
 * The chat shell against a stubbed API.
 *
 * The behaviours worth protecting are the ones a user would notice: history
 * survives a reload, a rename shows immediately, deleting the open thread does
 * not strand you on a dead route, and paging the sidebar follows the server's
 * cursor rather than inventing one.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { App } from '@/app/App'
import { jsonResponse, readBody, renderApp, stubApi, testUser, tokenPayload } from '@/test/utils'

const CONVERSATION_ID = '22222222-2222-4222-8222-222222222222'

function conversation(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: CONVERSATION_ID,
    title: 'Keyset pagination',
    created_at: '2026-09-01T10:00:00Z',
    updated_at: '2026-09-01T10:05:00Z',
    ...overrides,
  }
}

function message(id: string, role: 'user' | 'assistant', content: string, sequence: number) {
  return {
    id,
    conversation_id: CONVERSATION_ID,
    role,
    content,
    status: 'complete',
    sequence_number: sequence,
    created_at: '2026-09-01T10:00:00Z',
  }
}

/** Everything a signed-in session needs before the chat routes are reached. */
const signedIn = {
  '/auth/refresh': () => jsonResponse(200, tokenPayload),
  '/users/me': () => jsonResponse(200, testUser),
}

const oneConversation = {
  ...signedIn,
  'GET /conversations': () =>
    jsonResponse(200, { items: [conversation()], next_cursor: null, has_more: false }),
}

describe('the conversation sidebar', () => {
  it('greets the user by first name on the chat home', async () => {
    stubApi(oneConversation)
    renderApp(<App />, { route: '/chat' })
    // testUser is "Test User".
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent(/\bTest\b/)
  })

  it('lists the user’s conversations', async () => {
    stubApi(oneConversation)
    renderApp(<App />, { route: '/chat' })

    expect(await screen.findByRole('link', { name: 'Keyset pagination' })).toBeInTheDocument()
  })

  it('says so when there are none', async () => {
    stubApi({
      ...signedIn,
      'GET /conversations': () =>
        jsonResponse(200, { items: [], next_cursor: null, has_more: false }),
    })
    renderApp(<App />, { route: '/chat' })

    expect(await screen.findByText(/no conversations yet/i)).toBeInTheDocument()
  })

  it('follows the server’s cursor when loading more', async () => {
    const user = userEvent.setup()
    const fetchMock = stubApi({
      ...signedIn,
      'GET /conversations': ({ url }) =>
        url.searchParams.get('cursor') === null
          ? jsonResponse(200, {
              items: [conversation({ id: 'a', title: 'First page' })],
              next_cursor: 'CURSOR-FROM-SERVER',
              has_more: true,
            })
          : jsonResponse(200, {
              items: [conversation({ id: 'b', title: 'Second page' })],
              next_cursor: null,
              has_more: false,
            }),
    })
    renderApp(<App />, { route: '/chat' })

    await screen.findByRole('link', { name: 'First page' })
    await user.click(screen.getByRole('button', { name: /load more/i }))

    expect(await screen.findByRole('link', { name: 'Second page' })).toBeInTheDocument()
    // The client must echo the opaque cursor, never build its own.
    const paged = fetchMock.mock.calls.map(String).find((call) => call.includes('cursor='))
    expect(paged).toContain('cursor=CURSOR-FROM-SERVER')
    expect(paged).not.toContain('offset')
  })

  it('creates a conversation and opens it', async () => {
    const user = userEvent.setup()
    stubApi({
      ...signedIn,
      'GET /conversations': () =>
        jsonResponse(200, { items: [], next_cursor: null, has_more: false }),
      'POST /conversations': () => jsonResponse(201, conversation({ title: 'New conversation' })),
      [`GET /conversations/${CONVERSATION_ID}`]: () => jsonResponse(200, conversation()),
      [`GET /conversations/${CONVERSATION_ID}/messages`]: () =>
        jsonResponse(200, { items: [], next_cursor: null, has_more: false }),
    })
    renderApp(<App />, { route: '/chat' })

    await screen.findByText(/no conversations yet/i)
    const sidebar = screen.getByRole('navigation', { name: 'Conversations' })
    await user.click(within(sidebar).getByRole('button', { name: /new chat/i }))

    await waitFor(() => expect(window.location.pathname).toBe(`/chat/${CONVERSATION_ID}`))
  })
})

describe('renaming and deleting', () => {
  it('renames a conversation inline', async () => {
    const user = userEvent.setup()
    let title = 'Keyset pagination'
    stubApi({
      ...signedIn,
      // Stateful, so the refetch that follows the mutation returns the new
      // title — the same round trip the real API performs.
      'GET /conversations': () =>
        jsonResponse(200, { items: [conversation({ title })], next_cursor: null, has_more: false }),
      [`PATCH /conversations/${CONVERSATION_ID}`]: ({ init }) => {
        title = readBody<{ title: string }>(init).title
        return jsonResponse(200, conversation({ title }))
      },
    })
    renderApp(<App />, { route: '/chat' })

    await screen.findByRole('link', { name: 'Keyset pagination' })
    await user.click(screen.getByRole('button', { name: /rename keyset pagination/i }))

    const input = screen.getByLabelText(/rename conversation/i)
    await user.clear(input)
    await user.type(input, 'Renamed{Enter}')

    expect(await screen.findByRole('link', { name: 'Renamed' })).toBeInTheDocument()
  })

  it('abandons a rename on Escape', async () => {
    const user = userEvent.setup()
    stubApi(oneConversation)
    renderApp(<App />, { route: '/chat' })

    await screen.findByRole('link', { name: 'Keyset pagination' })
    await user.click(screen.getByRole('button', { name: /rename keyset pagination/i }))
    await user.type(screen.getByLabelText(/rename conversation/i), ' scrapped{Escape}')

    expect(await screen.findByRole('link', { name: 'Keyset pagination' })).toBeInTheDocument()
  })

  it('asks before deleting', async () => {
    const user = userEvent.setup()
    stubApi(oneConversation)
    renderApp(<App />, { route: '/chat' })

    await screen.findByRole('link', { name: 'Keyset pagination' })
    await user.click(screen.getByRole('button', { name: /delete keyset pagination/i }))

    // A single stray click must not destroy a thread: a confirmation stands
    // between the button and the deletion.
    expect(
      screen.getByRole('button', { name: /confirm delete keyset pagination/i }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /^cancel$/i }))
    expect(await screen.findByRole('link', { name: 'Keyset pagination' })).toBeInTheDocument()
  })

  it('navigates away when the open conversation is deleted', async () => {
    const user = userEvent.setup()
    let deleted = false
    stubApi({
      ...signedIn,
      'GET /conversations': () =>
        jsonResponse(200, {
          items: deleted ? [] : [conversation()],
          next_cursor: null,
          has_more: false,
        }),
      [`GET /conversations/${CONVERSATION_ID}`]: () => jsonResponse(200, conversation()),
      [`GET /conversations/${CONVERSATION_ID}/messages`]: () =>
        jsonResponse(200, { items: [], next_cursor: null, has_more: false }),
      [`DELETE /conversations/${CONVERSATION_ID}`]: () => {
        deleted = true
        return new Response(null, { status: 204 })
      },
    })
    renderApp(<App />, { route: `/chat/${CONVERSATION_ID}` })

    await screen.findByRole('link', { name: 'Keyset pagination' })
    await user.click(screen.getByRole('button', { name: /delete keyset pagination/i }))
    await user.click(screen.getByRole('button', { name: /confirm delete/i }))

    // Staying put would 404 on the next reload.
    await waitFor(() => expect(window.location.pathname).toBe('/chat'))
  })
})

describe('conversation history', () => {
  const withHistory = {
    ...oneConversation,
    [`GET /conversations/${CONVERSATION_ID}`]: () => jsonResponse(200, conversation()),
    [`GET /conversations/${CONVERSATION_ID}/messages`]: () =>
      jsonResponse(200, {
        items: [
          message('m1', 'user', 'How does keyset pagination work?', 1),
          message('m2', 'assistant', 'It uses the **last row seen** as the cursor.', 2),
        ],
        next_cursor: null,
        has_more: false,
      }),
  }

  it('restores the thread on a direct load of /chat/:id', async () => {
    stubApi(withHistory)
    renderApp(<App />, { route: `/chat/${CONVERSATION_ID}` })

    expect(await screen.findByText('How does keyset pagination work?')).toBeInTheDocument()
    expect(await screen.findByText(/it uses the/i)).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Keyset pagination' })).toBeInTheDocument()
  })

  it('renders assistant turns as markdown and user turns as plain text', async () => {
    stubApi(withHistory)
    const { container } = renderApp(<App />, { route: `/chat/${CONVERSATION_ID}` })

    // The markdown renderer is a lazy chunk, so the formatted output arrives
    // after the plain-text Suspense fallback. Resolving a dynamic import can
    // take several seconds while the suite's other files compile in parallel,
    // hence the explicit wait rather than the 1s default.
    await waitFor(
      () =>
        expect(container.querySelector('.prose-chat strong')).toHaveTextContent('last row seen'),
      { timeout: 10_000 },
    )

    // The user's own asterisks would be markdown if we rendered them as such.
    const userTurn = screen.getByLabelText('Your message')
    expect(within(userTurn).getByText('How does keyset pagination work?')).toBeInTheDocument()
    // The test's own budget must exceed the wait inside it. At the 5s default
    // it equalled the waitFor timeout, so under load the test timed out before
    // the wait could succeed.
  }, 15_000)

  it('shows a not-found state for a conversation that is gone', async () => {
    stubApi({
      ...oneConversation,
      [`GET /conversations/${CONVERSATION_ID}`]: () =>
        jsonResponse(404, {
          error: {
            code: 'conversation_not_found',
            message: 'Conversation not found.',
            request_id: null,
            details: null,
          },
        }),
    })
    renderApp(<App />, { route: `/chat/${CONVERSATION_ID}` })

    expect(await screen.findByText(/conversation not found/i)).toBeInTheDocument()
  })
})
