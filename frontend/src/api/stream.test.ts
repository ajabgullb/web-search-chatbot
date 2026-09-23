/**
 * The SSE reader against the way networks actually deliver bytes: split
 * mid-event, mid-line, mid-CRLF, and mid-character.
 */

import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'
import { SSEParser, StreamInterruptedError, parseRunEvent, streamRun } from '@/api/stream'
import type { ServerSentEvent } from '@/api/stream'
import type { RunEvent } from '@/api/types'
import { jsonResponse, stubApi, tokenPayload } from '@/test/utils'

const encoder = new TextEncoder()

function parse(chunks: Uint8Array[]): ServerSentEvent[] {
  const events: ServerSentEvent[] = []
  const parser = new SSEParser((event) => events.push(event))
  for (const chunk of chunks) parser.push(chunk)
  parser.end()
  return events
}

/** Every way of cutting `bytes` into two pieces. */
function everySplit(bytes: Uint8Array): Uint8Array[][] {
  const cuts: Uint8Array[][] = []
  for (let at = 0; at <= bytes.length; at += 1) {
    cuts.push([bytes.slice(0, at), bytes.slice(at)])
  }
  return cuts
}

const STREAM =
  ': heartbeat\n\n' +
  'id: 1\nevent: run.started\ndata: {"type":"run.started","n":1}\n\n' +
  'id: 2\nevent: message.delta\ndata: {"type":"message.delta","text":"café — 東京 🚀"}\n\n'

describe('SSEParser', () => {
  it('parses frames and ignores heartbeat comments', () => {
    const events = parse([encoder.encode(STREAM)])

    expect(events).toEqual([
      { event: 'run.started', data: '{"type":"run.started","n":1}', id: '1' },
      {
        event: 'message.delta',
        data: '{"type":"message.delta","text":"café — 東京 🚀"}',
        id: '2',
      },
    ])
  })

  it('gives the same result however the bytes are split, even inside a character', () => {
    const bytes = encoder.encode(STREAM)
    const whole = parse([bytes])

    for (const pieces of everySplit(bytes)) {
      expect(parse(pieces)).toEqual(whole)
    }
    // And the harshest case: one byte at a time.
    expect(parse(Array.from(bytes, (byte) => Uint8Array.of(byte)))).toEqual(whole)
  })

  it('never emits a replacement character for a split multi-byte sequence', () => {
    const bytes = encoder.encode('data: 🚀\n\n')
    for (const pieces of everySplit(bytes)) {
      const [event] = parse(pieces)
      expect(event?.data).toBe('🚀')
    }
  })

  it('accepts CRLF and CR line endings, including a CRLF split across chunks', () => {
    const crlf = encoder.encode('event: a\r\ndata: x\r\n\r\n')
    for (const pieces of everySplit(crlf)) {
      expect(parse(pieces)).toEqual([{ event: 'a', data: 'x', id: null }])
    }
    expect(parse([encoder.encode('data: y\r\r')])).toEqual([
      { event: 'message', data: 'y', id: null },
    ])
  })

  it('joins multiple data lines with newlines', () => {
    expect(parse([encoder.encode('data: one\ndata:two\ndata:  three\n\n')])).toEqual([
      { event: 'message', data: 'one\ntwo\n three', id: null },
    ])
  })

  it('discards an event that the stream cut off before its blank line', () => {
    expect(parse([encoder.encode('data: complete\n\ndata: cut off\n')])).toEqual([
      { event: 'message', data: 'complete', id: null },
    ])
  })

  it('ignores unknown fields and events with no data', () => {
    expect(parse([encoder.encode('retry: 10\nfoo: bar\n\nevent: empty\n\n')])).toEqual([])
  })
})

describe('parseRunEvent', () => {
  it('accepts known event types and skips everything else', () => {
    expect(parseRunEvent('{"type":"message.delta","text":"hi"}')).toMatchObject({ text: 'hi' })
    expect(parseRunEvent('{"type":"from.the.future"}')).toBeNull()
    expect(parseRunEvent('not json')).toBeNull()
    expect(parseRunEvent('"a string"')).toBeNull()
  })
})

/** A streaming Response delivering `body` in `chunkSize`-byte pieces. */
function chunkedResponse(body: string, chunkSize = 7): Response {
  const bytes = encoder.encode(body)
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let at = 0; at < bytes.length; at += chunkSize) {
        controller.enqueue(bytes.slice(at, at + chunkSize))
      }
      controller.close()
    },
  })
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

function frame(event: Record<string, unknown>): string {
  return `id: ${String(event.sequence)}\nevent: ${String(event.type)}\ndata: ${JSON.stringify(event)}\n\n`
}

const RUN = 'r1'
const COMPLETE_STREAM =
  frame({ sequence: 1, run_id: RUN, type: 'agent.status', status: 'thinking' }) +
  ': heartbeat\n\n' +
  frame({ sequence: 2, run_id: RUN, type: 'message.delta', text: 'Grüße ' }) +
  frame({ sequence: 3, run_id: RUN, type: 'message.delta', text: 'aus 東京' }) +
  frame({ sequence: 4, run_id: RUN, type: 'run.cancelled', reason: 'user_cancelled' })

describe('streamRun', () => {
  it('delivers typed events in order and resolves on the terminal event', async () => {
    const fetchMock = stubApi({
      'POST /conversations/c1/runs': () => chunkedResponse(COMPLETE_STREAM, 3),
    })
    const events: RunEvent[] = []

    await streamRun({
      conversationId: 'c1',
      body: { content: 'hi' },
      idempotencyKey: 'key-1',
      onEvent: (event) => events.push(event),
    })

    expect(events.map((event) => event.type)).toEqual([
      'agent.status',
      'message.delta',
      'message.delta',
      'run.cancelled',
    ])
    const text = events.flatMap((event) => (event.type === 'message.delta' ? [event.text] : []))
    expect(text.join('')).toBe('Grüße aus 東京')

    const init = fetchMock.mock.calls[0]?.[1]
    const headers = new Headers(init?.headers)
    expect(headers.get('Idempotency-Key')).toBe('key-1')
    expect(headers.get('Accept')).toBe('text/event-stream')
  })

  it('refreshes once on a 401 and replays with the same idempotency key', async () => {
    let attempts = 0
    const fetchMock = stubApi({
      '/auth/refresh': () => jsonResponse(200, tokenPayload),
      'POST /conversations/c1/runs': () => {
        attempts += 1
        return attempts === 1
          ? jsonResponse(401, { error: { code: 'authentication_required', message: 'x' } })
          : chunkedResponse(COMPLETE_STREAM)
      },
    })

    await streamRun({
      conversationId: 'c1',
      body: { content: 'hi' },
      idempotencyKey: 'same',
      onEvent: vi.fn(),
    })

    const keys = fetchMock.mock.calls
      .filter(([url]) => String(url).endsWith('/runs'))
      .map(([, init]) => new Headers(init?.headers).get('Idempotency-Key'))
    expect(keys).toEqual(['same', 'same'])
  })

  it('reports a refused run as an ApiError before any event', async () => {
    stubApi({
      'POST /conversations/c1/runs': () =>
        jsonResponse(409, {
          error: {
            code: 'run_in_progress',
            message: 'Busy.',
            request_id: null,
            details: { run_id: 'active-run' },
          },
        }),
    })
    const onEvent = vi.fn()

    const error = await streamRun({
      conversationId: 'c1',
      body: { content: 'hi' },
      idempotencyKey: 'k',
      onEvent,
    }).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).is('run_in_progress')).toBe(true)
    expect((error as ApiError).details).toEqual({ run_id: 'active-run' })
    expect(onEvent).not.toHaveBeenCalled()
  })

  it('treats a stream that ends without a terminal event as interrupted', async () => {
    stubApi({
      'POST /conversations/c1/runs': () =>
        chunkedResponse(frame({ sequence: 1, run_id: RUN, type: 'message.delta', text: 'half' })),
    })

    await expect(
      streamRun({
        conversationId: 'c1',
        body: { content: 'hi' },
        idempotencyKey: 'k',
        onEvent: vi.fn(),
      }),
    ).rejects.toBeInstanceOf(StreamInterruptedError)
  })
})
