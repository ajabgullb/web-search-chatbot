/**
 * Streaming a run into the thread, against a stubbed API and a stream the
 * test drives event by event.
 *
 * What these protect: the answer appears as it is written; the streamed text
 * is replaced by the canonical message with nothing duplicated or dropped;
 * stop really cancels on the server; retry never duplicates the question; a
 * reload mid-answer recovers it; and every state is announced once, not per
 * token.
 */

import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'

import { App } from '@/app/App'
import { runStoreTiming } from '@/features/chat/runStore'
import { controlledStream, eventsFor } from '@/test/sse'
import type { ControlledStream } from '@/test/sse'
import { jsonResponse, readBody, renderApp, stubApi, testUser, tokenPayload } from '@/test/utils'

const CONVERSATION_ID = '22222222-2222-4222-8222-222222222222'
const RUN_ID = '33333333-3333-4333-8333-333333333333'
// The Markdown renderer is a lazy chunk; its first import can be slow while
// other test files compile in parallel.
const LAZY = { timeout: 10_000 }

// Pay the Markdown chunk's one-time transform cost (it carries the syntax
// highlighter) before any test runs. Left to the first test that renders an
// answer, it blocks the worker long enough to time out the *next* test's
// first query — an ordering-dependent failure that says nothing about the UI.
beforeAll(async () => {
  await import('@/features/chat/Markdown')
}, 30_000)

type Message = {
  id: string
  conversation_id: string
  role: 'user' | 'assistant'
  content: string
  status: 'complete'
  sequence_number: number
  created_at: string
  degraded: boolean
  notice: string | null
  citations: Record<string, unknown>[]
}

function message(
  id: string,
  role: 'user' | 'assistant',
  content: string,
  sequence: number,
  extra: Partial<Message> = {},
): Message {
  return {
    id,
    conversation_id: CONVERSATION_ID,
    role,
    content,
    status: 'complete',
    sequence_number: sequence,
    created_at: '2026-09-24T10:00:00Z',
    degraded: false,
    notice: null,
    citations: [],
    ...extra,
  }
}

function citation(ordinal: number, sourceId: string, title: string, url: string) {
  return {
    ordinal,
    source_id: sourceId,
    url,
    display_url: url.replace(/^https?:\/\//, ''),
    title,
    publisher: null,
    snippet: `Snippet for ${title}.`,
    published_at: null,
    retrieved_at: '2026-09-24T10:00:00Z',
  }
}

function runRecord(status: string, extra: Record<string, unknown> = {}) {
  return {
    id: RUN_ID,
    conversation_id: CONVERSATION_ID,
    user_message_id: 'u1',
    assistant_message_id: null,
    status,
    error: null,
    cancel_reason: null,
    search_count: 0,
    usage: { prompt_tokens: null, completion_tokens: null },
    created_at: '2026-09-24T10:00:00Z',
    started_at: '2026-09-24T10:00:00Z',
    completed_at: null,
    assistant_message: null,
    ...extra,
  }
}

/**
 * A small stateful backend: the history grows as the "server" stores
 * messages, so every refetch the UI makes sees a consistent world.
 */
function backend(options: { history?: Message[]; activeRunId?: string | null } = {}) {
  const state = {
    history: [...(options.history ?? [])],
    activeRunId: options.activeRunId ?? null,
    streams: [] as ControlledStream[],
    runBodies: [] as Record<string, unknown>[],
    runKeys: [] as (string | null)[],
    cancelled: [] as string[],
  }
  const routes = {
    '/auth/refresh': () => jsonResponse(200, tokenPayload),
    '/users/me': () => jsonResponse(200, testUser),
    'GET /conversations': () =>
      jsonResponse(200, {
        items: [
          {
            id: CONVERSATION_ID,
            title: 'Rates',
            created_at: '2026-09-24T10:00:00Z',
            updated_at: '2026-09-24T10:00:00Z',
          },
        ],
        next_cursor: null,
        has_more: false,
      }),
    [`GET /conversations/${CONVERSATION_ID}`]: () =>
      jsonResponse(200, {
        id: CONVERSATION_ID,
        title: 'Rates',
        created_at: '2026-09-24T10:00:00Z',
        updated_at: '2026-09-24T10:00:00Z',
        active_run_id: state.activeRunId,
      }),
    [`GET /conversations/${CONVERSATION_ID}/messages`]: () =>
      jsonResponse(200, { items: state.history, next_cursor: null, has_more: false }),
    [`POST /conversations/${CONVERSATION_ID}/runs`]: ({
      init,
    }: {
      init: RequestInit | undefined
    }) => {
      state.runBodies.push(readBody(init))
      state.runKeys.push(new Headers(init?.headers).get('Idempotency-Key'))
      const stream = controlledStream(init?.signal)
      state.streams.push(stream)
      return stream.response
    },
  }
  return { state, routes }
}

/** Mirrors what the real server does when it accepts and finishes a run. */
function acceptQuestion(
  state: ReturnType<typeof backend>['state'],
  stream: ControlledStream,
  ev: ReturnType<typeof eventsFor>,
  question: Message,
) {
  if (!state.history.some((m) => m.id === question.id)) state.history.push(question)
  stream.send(ev('run.started', { conversation_id: CONVERSATION_ID, user_message: question }))
}

function finish(
  state: ReturnType<typeof backend>['state'],
  stream: ControlledStream,
  ev: ReturnType<typeof eventsFor>,
  answer: Message,
) {
  state.history.push(answer)
  stream.send(ev('message.completed', { message: answer, run: runRecord('completed') }))
  stream.close()
}

async function openConversation() {
  renderApp(<App />, { route: `/chat/${CONVERSATION_ID}` })
  await screen.findByRole('heading', { name: 'Rates' })
}

async function send(text: string) {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText(/^message$/i), `${text}{Enter}`)
  return user
}

function latestStream(state: ReturnType<typeof backend>['state']): ControlledStream {
  const stream = state.streams.at(-1)
  if (!stream) throw new Error('no run was started')
  return stream
}

describe('streaming an answer', () => {
  it('shows the question at once, the answer as it is written, and the canonical message at the end', async () => {
    const { state, routes } = backend()
    stubApi(routes)
    await openConversation()

    await send('What changed in rates?')

    // Optimistic: on screen before the server has said anything.
    expect(screen.getByText('What changed in rates?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /stop answering/i })).toBeInTheDocument()
    await waitFor(() => expect(state.streams).toHaveLength(1))
    expect(state.runBodies[0]).toEqual({ content: 'What changed in rates?' })

    const stream = latestStream(state)
    const ev = eventsFor(RUN_ID)
    act(() => {
      acceptQuestion(state, stream, ev, message('u1', 'user', 'What changed in rates?', 1))
      stream.send(ev('agent.status', { status: 'thinking' }))
    })
    expect(await screen.findByText('The assistant is thinking.')).toBeInTheDocument()

    act(() => stream.send(ev('message.delta', { text: 'Rates rose ' }), 3))
    expect(await screen.findByText(/Rates rose/)).toBeInTheDocument()
    expect(screen.queryByText(/in March/)).not.toBeInTheDocument()

    act(() => {
      stream.heartbeat()
      stream.send(ev('message.delta', { text: 'in March.' }), 5)
    })
    expect(await screen.findByText('Rates rose in March.')).toBeInTheDocument()

    act(() => finish(state, stream, ev, message('a1', 'assistant', 'Rates rose in March.', 2)))

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /stop answering/i })).not.toBeInTheDocument(),
    )
    // The streamed copy is gone and the canonical one is there: exactly once
    // each, question and answer — nothing duplicated, nothing dropped.
    expect(screen.getAllByText('Rates rose in March.')).toHaveLength(1)
    expect(screen.getAllByText('What changed in rates?')).toHaveLength(1)
    expect(screen.getByText('Answer complete.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /send message/i })).toBeInTheDocument()
  })

  it('renders inline citations that jump to safe source links', async () => {
    const { state, routes } = backend()
    stubApi(routes)
    await openConversation()
    const user = await send('latest rates?')
    await waitFor(() => expect(state.streams).toHaveLength(1))
    const stream = latestStream(state)
    const ev = eventsFor(RUN_ID)

    const sources = [
      citation(1, 'S1', 'Central bank statement', 'https://bank.test/statement'),
      citation(2, 'S2', 'Suspicious page', 'javascript:alert(1)'),
    ]
    act(() => {
      acceptQuestion(state, stream, ev, message('u1', 'user', 'latest rates?', 1))
      stream.send(ev('agent.status', { status: 'searching' }))
      stream.send(ev('search.started', { query: 'latest rates?' }))
      stream.send(
        ev('search.completed', {
          query: 'latest rates?',
          failed: false,
          sources: sources.map(({ snippet: _s, ordinal: _o, retrieved_at: _r, ...rest }) => rest),
        }),
      )
      stream.send(ev('citation', { citation: sources[0] }))
      stream.send(ev('message.delta', { text: 'Rates rose [S1]. See `[S1]` in code.' }))
    })

    // Progress while it streams: the search, and the source it found.
    expect(await screen.findByText(/searched “latest rates\?”/i)).toBeInTheDocument()
    expect(screen.getByText('Searching the web.')).toBeInTheDocument()
    const liveMarker = await screen.findByRole('button', { name: /source 1: central bank/i }, LAZY)
    expect(liveMarker).toBeInTheDocument()

    act(() => {
      stream.send(ev('citation', { citation: sources[1] }))
      finish(
        state,
        stream,
        ev,
        message('a1', 'assistant', 'Rates rose [S1]. Also [S2]. See `[S1]` in code.', 2, {
          citations: sources,
        }),
      )
    })

    const answer = await screen.findByRole('article', { name: 'Assistant message' })
    const marker = await within(answer).findByRole(
      'button',
      { name: /source 1: central bank/i },
      LAZY,
    )
    // The marker inside inline code stayed code.
    expect(within(answer).getByText('[S1]', { selector: 'code' })).toBeInTheDocument()

    const link = within(answer).getByRole('link', { name: /central bank statement/i })
    expect(link).toHaveAttribute('href', 'https://bank.test/statement')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    // A javascript: URL is shown as text, never as a link.
    expect(within(answer).getByText('Suspicious page')).toBeInTheDocument()
    expect(within(answer).queryByRole('link', { name: /suspicious page/i })).not.toBeInTheDocument()

    await user.click(marker)
    expect(document.activeElement).toHaveTextContent('Central bank statement')
  }, 20_000)

  it('flags a degraded answer with its notice', async () => {
    const { state, routes } = backend()
    stubApi(routes)
    await openConversation()
    await send('latest rates?')
    await waitFor(() => expect(state.streams).toHaveLength(1))
    const stream = latestStream(state)
    const ev = eventsFor(RUN_ID)

    act(() => {
      acceptQuestion(state, stream, ev, message('u1', 'user', 'latest rates?', 1))
      finish(
        state,
        stream,
        ev,
        message('a1', 'assistant', 'Probably about 5%.', 2, {
          degraded: true,
          notice: 'Web search was unavailable for this answer.',
        }),
      )
    })

    expect(
      await screen.findByText('Web search was unavailable for this answer.'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('Answer complete. It could not be verified against sources.'),
    ).toBeInTheDocument()
  })
})

describe('stopping and retrying', () => {
  it('stop cancels the run on the server, and retry answers the same question again', async () => {
    const { state, routes } = backend()
    const fetchMock = stubApi({
      ...routes,
      [`POST /runs/${RUN_ID}/cancel`]: () => {
        state.cancelled.push(RUN_ID)
        // What the server does: the executor publishes run.cancelled.
        const stream = latestStream(state)
        stream.send({
          sequence: 99,
          run_id: RUN_ID,
          type: 'run.cancelled',
          reason: 'user_cancelled',
        } as never)
        stream.close()
        return jsonResponse(200, runRecord('cancelled', { cancel_reason: 'user_cancelled' }))
      },
    })
    await openConversation()
    const user = await send('Explain rates')
    await waitFor(() => expect(state.streams).toHaveLength(1))
    const stream = latestStream(state)
    const ev = eventsFor(RUN_ID)
    act(() => {
      acceptQuestion(state, stream, ev, message('u1', 'user', 'Explain rates', 1))
      stream.send(ev('message.delta', { text: 'Rates are' }))
    })
    await screen.findByText(/Rates are/)

    await user.click(screen.getByRole('button', { name: /stop answering/i }))

    // Stopped on the server, not merely hidden.
    await waitFor(() => expect(state.cancelled).toEqual([RUN_ID]))
    expect(await screen.findByText('Stopped.')).toBeInTheDocument()
    expect(screen.getByText('Answer stopped.')).toBeInTheDocument()
    // The partial text stays visible, marked as stopped.
    expect(screen.getByText(/Rates are/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /^retry$/i }))
    await waitFor(() => expect(state.streams).toHaveLength(2))
    expect(state.runBodies[1]).toEqual({ retry_message_id: 'u1' })
    expect(state.runKeys[1]).not.toBe(state.runKeys[0])

    const retryStream = latestStream(state)
    const retryEv = eventsFor('run-2')
    act(() => {
      acceptQuestion(state, retryStream, retryEv, message('u1', 'user', 'Explain rates', 1))
      finish(
        state,
        retryStream,
        retryEv,
        message('a2', 'assistant', 'Rates are the price of money.', 2),
      )
    })

    expect(await screen.findByText('Rates are the price of money.')).toBeInTheDocument()
    // One question, however many attempts it took.
    expect(screen.getAllByText('Explain rates')).toHaveLength(1)
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/cancel'))).toHaveLength(1)
  })

  it('shows a failed run with its reason and a retry', async () => {
    const { state, routes } = backend()
    stubApi(routes)
    await openConversation()
    const user = await send('hi')
    await waitFor(() => expect(state.streams).toHaveLength(1))
    const stream = latestStream(state)
    const ev = eventsFor(RUN_ID)

    act(() => {
      acceptQuestion(state, stream, ev, message('u1', 'user', 'hi', 1))
      stream.send(
        ev('run.failed', {
          error: {
            code: 'llm_unavailable',
            message: 'The language model is unavailable. Please try again.',
            retryable: true,
          },
        }),
      )
      stream.close()
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The answer failed')
    expect(alert).toHaveTextContent('The language model is unavailable.')
    await user.click(screen.getByRole('button', { name: /^retry$/i }))
    await waitFor(() => expect(state.runBodies.at(-1)).toEqual({ retry_message_id: 'u1' }))
  })

  it('marks a refused message as not sent and resends it with the same idempotency key', async () => {
    const { state, routes } = backend()
    let attempts = 0
    stubApi({
      ...routes,
      [`POST /conversations/${CONVERSATION_ID}/runs`]: (request) => {
        attempts += 1
        if (attempts === 1) {
          state.runKeys.push(new Headers(request.init?.headers).get('Idempotency-Key'))
          return jsonResponse(409, {
            error: {
              code: 'run_in_progress',
              message: 'Busy.',
              request_id: null,
              details: { run_id: 'other' },
            },
          })
        }
        return routes[`POST /conversations/${CONVERSATION_ID}/runs`](request)
      },
    })
    await openConversation()
    const user = await send('Are you there?')

    const bubble = screen.getByRole('article', { name: 'Your message' })
    expect(await within(bubble).findByText(/not sent/i)).toHaveTextContent(
      'This conversation is still answering a previous message.',
    )
    // Announced as well as shown.
    expect(screen.getByText(/^Message not sent\./)).toHaveClass('sr-only', { exact: false })
    // The words the user typed are still on screen, not lost.
    expect(screen.getByText('Are you there?')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /try again/i }))
    await waitFor(() => expect(state.runKeys).toHaveLength(2))
    // Same key: had the first attempt reached the server after all, the
    // server would replay it instead of storing the question twice.
    expect(state.runKeys[1]).toBe(state.runKeys[0])
    expect(state.runBodies.at(-1)).toEqual({ content: 'Are you there?' })
  })

  it('treats a send that never reached the server as not sent', async () => {
    const { routes } = backend()
    stubApi({
      ...routes,
      [`POST /conversations/${CONVERSATION_ID}/runs`]: () => {
        throw new TypeError('Failed to fetch')
      },
    })
    await openConversation()
    await send('hello?')

    const bubble = screen.getByRole('article', { name: 'Your message' })
    expect(await within(bubble).findByText(/not sent/i)).toHaveTextContent(
      /could not reach the server/i,
    )
  })
})

describe('recovering after a reload', () => {
  const originalInterval = runStoreTiming.pollIntervalMs
  beforeEach(() => {
    runStoreTiming.pollIntervalMs = 20
  })
  afterEach(() => {
    runStoreTiming.pollIntervalMs = originalInterval
  })

  it('follows a run still in progress and shows its answer when it finishes', async () => {
    const question = message('u1', 'user', 'What happened today?', 1)
    const { state, routes } = backend({ history: [question], activeRunId: RUN_ID })
    let polls = 0
    const answer = message('a1', 'assistant', 'Here is what happened.', 2)
    stubApi({
      ...routes,
      [`GET /runs/${RUN_ID}`]: () => {
        polls += 1
        if (polls < 3) return jsonResponse(200, runRecord('running'))
        state.history.push(answer)
        state.activeRunId = null
        return jsonResponse(
          200,
          runRecord('completed', { assistant_message: answer, assistant_message_id: 'a1' }),
        )
      },
    })
    await openConversation()

    expect(await screen.findByText('Reconnecting to the answer…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /stop answering/i })).toBeInTheDocument()

    expect(await screen.findByText('Here is what happened.')).toBeInTheDocument()
    expect(screen.getAllByText('Here is what happened.')).toHaveLength(1)
    expect(screen.queryByText('Reconnecting to the answer…')).not.toBeInTheDocument()
    expect(polls).toBeGreaterThanOrEqual(3)
  })

  it('offers a retry for a question left without an answer', async () => {
    const { state, routes } = backend({ history: [message('u1', 'user', 'Still there?', 1)] })
    stubApi(routes)
    await openConversation()

    const notice = await screen.findByRole('article', { name: 'No answer' })
    await userEvent.setup().click(within(notice).getByRole('button', { name: /retry/i }))

    await waitFor(() => expect(state.runBodies).toEqual([{ retry_message_id: 'u1' }]))
  })
})
