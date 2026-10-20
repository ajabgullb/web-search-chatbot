/**
 * Attaching PDFs from the composer, against a stubbed API.
 *
 * What a user would notice if it broke: the button is where they type, a
 * picked PDF shows up and becomes ready on its own, a wrong file is refused
 * before it is sent, a server refusal is explained, and removing works.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { App } from '@/app/App'
import { MAX_DOCUMENT_BYTES } from '@/features/chat/constants'
import { jsonResponse, renderApp, stubApi, testUser, tokenPayload } from '@/test/utils'

const CONVERSATION_ID = '22222222-2222-4222-8222-222222222222'
const DOCUMENT_ID = '33333333-3333-4333-8333-333333333333'
const DOCUMENTS = `/conversations/${CONVERSATION_ID}/documents`

function document(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: DOCUMENT_ID,
    filename: 'report.pdf',
    status: 'ready',
    error_code: null,
    byte_size: 1024,
    page_count: 3,
    chunk_count: 4,
    created_at: '2026-10-20T10:00:00Z',
    updated_at: '2026-10-20T10:00:00Z',
    ...overrides,
  }
}

const conversation = {
  id: CONVERSATION_ID,
  title: 'Quarterly report',
  created_at: '2026-10-20T09:00:00Z',
  updated_at: '2026-10-20T09:00:00Z',
}

/** A signed-in user looking at one empty conversation. */
function pageRoutes(documents: () => unknown[]) {
  return {
    '/auth/refresh': () => jsonResponse(200, tokenPayload),
    '/users/me': () => jsonResponse(200, testUser),
    'GET /conversations': () =>
      jsonResponse(200, { items: [conversation], next_cursor: null, has_more: false }),
    [`GET /conversations/${CONVERSATION_ID}`]: () => jsonResponse(200, conversation),
    [`GET /conversations/${CONVERSATION_ID}/messages`]: () =>
      jsonResponse(200, { items: [], next_cursor: null, has_more: false }),
    [`GET ${DOCUMENTS}`]: () => jsonResponse(200, { items: documents() }),
  }
}

function pdf(name = 'report.pdf'): File {
  return new File(['%PDF-1.4 test'], name, { type: 'application/pdf' })
}

async function openConversation() {
  renderApp(<App />, { route: `/chat/${CONVERSATION_ID}` })
  return screen.findByRole('button', { name: /attach a pdf/i })
}

function fileInput(): HTMLInputElement {
  return screen.getByTestId('composer-file-input')
}

function uploads(fetchMock: ReturnType<typeof stubApi>) {
  return fetchMock.mock.calls.filter(
    ([url, init]) => String(url).endsWith(DOCUMENTS) && init?.method === 'POST',
  )
}

describe('attaching a PDF', () => {
  it('puts the attach button inside the message box', async () => {
    stubApi(pageRoutes(() => []))
    const button = await openConversation()

    const textbox = screen.getByLabelText(/^message$/i)
    // Same bordered box: the nearest common container holds both.
    expect(button.closest('form')).toBe(textbox.closest('form'))
    expect(button.parentElement).toBe(textbox.parentElement)
  })

  it('uploads the file as multipart and shows it until it is ready', async () => {
    const user = userEvent.setup()
    let stored: ReturnType<typeof document>[] = []
    let polls = 0
    const fetchMock = stubApi({
      ...pageRoutes(() => {
        // Preparing on the first look after the upload, ready on the next:
        // the chip must get there by polling, with no further action.
        if (stored.length > 0) polls += 1
        return polls > 1 ? stored.map((d) => ({ ...d, status: 'ready' })) : stored
      }),
      [`POST ${DOCUMENTS}`]: () => {
        stored = [document({ status: 'processing' })]
        return jsonResponse(202, stored[0])
      },
    })
    await openConversation()

    await user.upload(fileInput(), pdf())

    const calls = uploads(fetchMock)
    expect(calls).toHaveLength(1)
    const init: RequestInit = calls[0]?.[1] ?? {}
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.body as FormData).get('file')).toBeInstanceOf(File)
    // The browser must set the multipart boundary itself.
    expect(new Headers(init.headers).get('Content-Type')).toBeNull()

    const list = await screen.findByRole('list', { name: /attached documents/i })
    expect(await within(list).findByText(/preparing/)).toBeInTheDocument()
    expect(await within(list).findByText(/ready/, {}, { timeout: 4_000 })).toBeInTheDocument()
    expect(within(list).getByText('report.pdf')).toBeInTheDocument()
  })

  it('refuses a file that is not a PDF without sending it', async () => {
    // applyAccept off: the picker filter is a convenience a user can bypass,
    // so the check behind it is what is under test.
    const user = userEvent.setup({ applyAccept: false })
    const fetchMock = stubApi(pageRoutes(() => []))
    await openConversation()

    await user.upload(fileInput(), new File(['hello'], 'notes.txt', { type: 'text/plain' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/notes\.txt is not a pdf/i)
    expect(uploads(fetchMock)).toHaveLength(0)
  })

  it('refuses a file over the size limit without sending it', async () => {
    const user = userEvent.setup()
    const fetchMock = stubApi(pageRoutes(() => []))
    await openConversation()
    const huge = pdf('scan.pdf')
    Object.defineProperty(huge, 'size', { value: MAX_DOCUMENT_BYTES + 1 })

    await user.upload(fileInput(), huge)

    expect(await screen.findByRole('alert')).toHaveTextContent(/scan\.pdf is larger than 10 MB/i)
    expect(uploads(fetchMock)).toHaveLength(0)
  })

  it('explains a refusal from the server', async () => {
    const user = userEvent.setup()
    stubApi({
      ...pageRoutes(() => []),
      [`POST ${DOCUMENTS}`]: () =>
        jsonResponse(422, {
          error: {
            code: 'pdf_encrypted',
            message: 'The PDF is password-protected. Upload a copy without a password.',
            request_id: 'r1',
            details: null,
          },
        }),
    })
    await openConversation()

    await user.upload(fileInput(), pdf('locked.pdf'))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not attach locked.pdf: The PDF is password-protected.',
    )
    await user.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('attached documents', () => {
  it('shows a document that could not be prepared as failed', async () => {
    stubApi(pageRoutes(() => [document({ status: 'failed', error_code: 'embedding_unavailable' })]))
    await openConversation()

    const list = await screen.findByRole('list', { name: /attached documents/i })
    expect(within(list).getByText(/failed/)).toBeInTheDocument()
  })

  it('removes a document', async () => {
    const user = userEvent.setup()
    let stored = [document()]
    const fetchMock = stubApi({
      ...pageRoutes(() => stored),
      [`DELETE ${DOCUMENTS}/${DOCUMENT_ID}`]: () => {
        stored = []
        return new Response(null, { status: 204 })
      },
    })
    await openConversation()

    await user.click(await screen.findByRole('button', { name: 'Remove report.pdf' }))

    await waitFor(() =>
      expect(screen.queryByRole('list', { name: /attached documents/i })).not.toBeInTheDocument(),
    )
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => String(url).endsWith(DOCUMENT_ID) && init?.method === 'DELETE',
      ),
    ).toBe(true)
  })
})
