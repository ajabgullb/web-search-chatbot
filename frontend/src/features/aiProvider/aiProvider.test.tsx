/**
 * The model picker in the chat box and the AI provider page (ADR-0020),
 * against a stubbed API.
 *
 * What a user would notice if it broke: the picker shows the model in use,
 * switches with a click or the keyboard, accepts a typed model name, asks for
 * a key right there when a provider has none, never sends the message while
 * it is open, and the key is never shown back.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { LlmSettings } from '@/api/types'
import { App } from '@/app/App'
import {
  defaultLlmSettings,
  errorResponse,
  jsonResponse,
  readBody,
  renderApp,
  stubApi,
  testUser,
  tokenPayload,
} from '@/test/utils'

const KEY = 'sk-ant-api03-test-key-ending-wxyz'
const PAGE = '/settings/ai-provider'
const NOW = '2026-11-10T10:00:00Z'

function credential(provider: string, model: string, active: boolean, hint = 'abcd') {
  return {
    provider,
    model,
    key_hint: hint,
    is_active: active,
    verified_at: NOW,
    updated_at: NOW,
  }
}

/** A tiny in-memory version of the server's settings endpoints. */
function fakeServer(initial: Partial<LlmSettings> = {}) {
  let settings = { ...(defaultLlmSettings as LlmSettings), ...initial }

  function reselect(): LlmSettings['selection'] {
    const active = settings.credentials.find((item) => item.is_active)
    if (active) return { source: 'user', provider: active.provider, model: active.model }
    if (settings.server_model) {
      return { source: 'server', provider: null, model: settings.server_model.model }
    }
    return null
  }

  const routes = {
    '/auth/refresh': () => jsonResponse(200, tokenPayload),
    '/users/me': () => jsonResponse(200, testUser),
    'GET /conversations': () =>
      jsonResponse(200, { items: [], next_cursor: null, has_more: false }),
    'GET /users/me/llm-settings': () => jsonResponse(200, settings),
    'PUT /users/me/llm-selection': ({ init }: { init?: RequestInit | undefined }) => {
      const body = readBody<{ provider: string | null; model: string | null }>(init)
      settings = {
        ...settings,
        credentials: settings.credentials.map((item) => ({
          ...item,
          is_active: item.provider === body.provider,
          model: item.provider === body.provider && body.model ? body.model : item.model,
        })),
      }
      settings.selection = reselect()
      return jsonResponse(200, settings)
    },
    'PUT /users/me/llm-credentials/anthropic': ({ init }: { init?: RequestInit | undefined }) => {
      const body = readBody<{ model: string; api_key: string }>(init)
      settings = {
        ...settings,
        credentials: [
          ...settings.credentials
            .filter((item) => item.provider !== 'anthropic')
            .map((item) => ({ ...item, is_active: false })),
          credential('anthropic', body.model, true, body.api_key.slice(-4)),
        ],
      }
      settings.selection = reselect()
      return jsonResponse(200, settings)
    },
    'DELETE /users/me/llm-credentials/openai': () => {
      settings = {
        ...settings,
        credentials: settings.credentials.filter((item) => item.provider !== 'openai'),
      }
      settings.selection = reselect()
      return new Response(null, { status: 204 })
    },
  }
  return { routes }
}

function requestsTo(fetchMock: ReturnType<typeof stubApi>, method: string, path: string) {
  return fetchMock.mock.calls
    .filter(([input, init]) => (init?.method ?? 'GET') === method && String(input).includes(path))
    .map(([, init]) => readBody<Record<string, unknown>>(init))
}

async function openPicker() {
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: /^Model:/ }))
  return user
}

describe('model picker', () => {
  it('shows the model in use and lists every model to choose from', async () => {
    stubApi(fakeServer().routes)
    renderApp(<App />, { route: '/chat' })

    const trigger = await screen.findByRole('button', { name: /^Model: gpt-4o-mini/ })
    expect(trigger).toHaveTextContent('Default')
    await openPicker()

    const list = screen.getByRole('listbox', { name: 'Models' })
    expect(within(list).getByRole('option', { name: 'gpt-4o-mini' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(within(list).getByRole('group', { name: /Anthropic/ })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Search models' })).toHaveFocus()
  })

  it('switches models from the keyboard and hands focus back', async () => {
    const fetchMock = stubApi(
      fakeServer({
        credentials: [credential('openai', 'gpt-4.1-mini', false)],
      }).routes,
    )
    renderApp(<App />, { route: '/chat' })

    const trigger = await screen.findByRole('button', { name: /^Model:/ })
    trigger.focus()
    const user = userEvent.setup()
    // Arrow keys open it on the model in use (the default), then move down
    // to the first OpenAI model.
    await user.keyboard('{ArrowDown}')
    await user.keyboard('{ArrowDown}{Enter}')

    expect(
      await screen.findByRole('button', { name: /^Model: gpt-4.1-mini \(OpenAI\)/ }),
    ).toHaveFocus()
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(requestsTo(fetchMock, 'PUT', '/llm-selection')).toEqual([
      { provider: 'openai', model: 'gpt-4.1-mini' },
    ])
    expect(await screen.findByText('Answers will now use gpt-4.1-mini.')).toBeInTheDocument()
  })

  it('uses a model name typed into the search', async () => {
    const fetchMock = stubApi(
      fakeServer({ credentials: [credential('openai', 'gpt-4.1-mini', true)] }).routes,
    )
    renderApp(<App />, { route: '/chat' })

    const user = await openPicker()
    await user.type(screen.getByRole('combobox'), 'o3-mini')
    expect(screen.getByRole('option', { name: 'Use “o3-mini”' })).toBeInTheDocument()
    await user.keyboard('{Enter}')

    await waitFor(() =>
      expect(requestsTo(fetchMock, 'PUT', '/llm-selection')).toEqual([
        { provider: 'openai', model: 'o3-mini' },
      ]),
    )
  })

  it('asks for a key right there when a provider has none, then switches to it', async () => {
    const fetchMock = stubApi(fakeServer().routes)
    renderApp(<App />, { route: '/chat' })

    const user = await openPicker()
    await user.click(screen.getByRole('option', { name: /claude-haiku-5-5/ }))

    expect(screen.getByRole('group', { name: 'Connect Anthropic' })).toBeInTheDocument()
    const keyField = screen.getByLabelText('API key')
    expect(keyField).toHaveFocus()
    await user.type(keyField, `${KEY}{Enter}`)

    expect(
      await screen.findByRole('button', { name: /^Model: claude-haiku-5-5 \(Anthropic\)/ }),
    ).toBeInTheDocument()
    expect(requestsTo(fetchMock, 'PUT', '/llm-credentials/anthropic')).toEqual([
      { model: 'claude-haiku-5-5', api_key: KEY },
    ])
    expect(document.body.textContent).not.toContain(KEY)
  })

  it('shows why a key was refused, and Escape steps back then closes', async () => {
    stubApi({
      ...fakeServer().routes,
      'PUT /users/me/llm-credentials/anthropic': () =>
        errorResponse(422, 'llm_key_invalid', 'The provider rejected this API key.'),
    })
    renderApp(<App />, { route: '/chat' })

    const user = await openPicker()
    await user.click(screen.getByRole('option', { name: /claude-sonnet-5-5/ }))
    await user.type(screen.getByLabelText('API key'), `${KEY}{Enter}`)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The provider rejected this API key.',
    )

    await user.keyboard('{Escape}')
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Model:/ })).toHaveFocus()
  })

  it('never sends the message while choosing a model', async () => {
    const fetchMock = stubApi(fakeServer().routes)
    renderApp(<App />, { route: '/chat' })

    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/^message$/i), 'Hello there')
    await user.click(screen.getByRole('button', { name: /^Model:/ }))
    await user.keyboard('{Enter}')

    expect(requestsTo(fetchMock, 'POST', '/conversations')).toEqual([])
    expect(screen.getByLabelText(/^message$/i)).toHaveValue('Hello there')
  })

  it('asks for a model when there is none to answer with', async () => {
    stubApi(fakeServer({ server_model: null, selection: null }).routes)
    renderApp(<App />, { route: '/chat' })

    expect(
      await screen.findByRole('button', { name: /^Model: Choose a model/ }),
    ).toBeInTheDocument()
    await openPicker()
    expect(screen.queryByRole('group', { name: /App default/ })).not.toBeInTheDocument()
  })
})

describe('AI provider page', () => {
  it('is reachable from the picker and the sidebar', async () => {
    stubApi(fakeServer().routes)
    renderApp(<App />, { route: '/chat' })

    const user = await openPicker()
    await user.click(screen.getByRole('link', { name: 'Manage keys' }))
    expect(await screen.findByRole('heading', { name: 'AI provider' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'AI provider' })).toHaveAttribute('href', PAGE)
  })

  it('connects a provider with a model of the user’s choosing', async () => {
    const user = userEvent.setup()
    const fetchMock = stubApi(fakeServer().routes)
    renderApp(<App />, { route: PAGE })

    await user.click(await screen.findByRole('button', { name: 'Connect Anthropic' }))
    const model = screen.getByLabelText('Model')
    expect(model).toHaveValue('claude-sonnet-5-5')
    await user.clear(model)
    await user.type(model, 'claude-opus-5-5')
    await user.type(screen.getByLabelText('API key'), KEY)
    await user.click(screen.getByRole('button', { name: 'Check and save' }))

    expect(await screen.findByText('In use')).toBeInTheDocument()
    expect(requestsTo(fetchMock, 'PUT', '/llm-credentials/anthropic')).toEqual([
      { model: 'claude-opus-5-5', api_key: KEY },
    ])
    expect(screen.getByText(/Answering with/)).toHaveTextContent('claude-opus-5-5 from Anthropic')
  })

  it('switches to a saved key and removes one after asking', async () => {
    const user = userEvent.setup()
    const fetchMock = stubApi(
      fakeServer({ credentials: [credential('openai', 'gpt-4o', false)] }).routes,
    )
    renderApp(<App />, { route: PAGE })

    await user.click(await screen.findByRole('button', { name: 'Use OpenAI' }))
    expect(await screen.findByText('In use')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Remove OpenAI key' }))
    expect(screen.getByText(/default model, gpt-4o-mini/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Confirm remove OpenAI key' }))

    await waitFor(() => expect(screen.getAllByText('Not connected')).toHaveLength(2))
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(true)
  })
})
