/**
 * Reading the run stream: Server-Sent Events over a POST.
 *
 * The browser's `EventSource` only speaks GET and cannot send an
 * `Authorization` header, and a run is started by POSTing the message with a
 * bearer token. So the stream is read with `fetch` and parsed here.
 *
 * The network delivers bytes in arbitrary pieces. Nothing guarantees a chunk
 * ends at the end of an event, a line, or even a character: "é" is two bytes in
 * UTF-8 and can arrive one byte per chunk. The parser therefore works in two
 * stages, each with its own carry-over:
 *
 * 1. `TextDecoder` in streaming mode holds back an incomplete multi-byte
 *    sequence until the rest of it arrives, so no character is ever mangled.
 * 2. Decoded text is buffered until a line ending, and lines are collected
 *    until the blank line that ends an event.
 *
 * Parsing follows the WHATWG event-stream rules: `data` lines join with "\n",
 * a single space after the colon is dropped, lines starting with ":" are
 * comments (our heartbeats), and LF, CRLF, and CR all end a line.
 */

import { openStream } from '@/api/client'
import { NetworkError } from '@/api/errors'
import type { RunCreate, RunEvent } from '@/api/types'

export interface ServerSentEvent {
  event: string
  data: string
  id: string | null
}

export class SSEParser {
  private readonly decoder = new TextDecoder('utf-8')
  private buffer = ''
  private dataLines: string[] = []
  private eventType = ''
  private lastEventId: string | null = null
  private readonly onEvent: (event: ServerSentEvent) => void

  constructor(onEvent: (event: ServerSentEvent) => void) {
    this.onEvent = onEvent
  }

  /** Feed the next chunk of bytes, however it happens to be cut. */
  push(chunk: Uint8Array): void {
    this.consume(this.decoder.decode(chunk, { stream: true }))
  }

  /**
   * The stream has ended. Flushes the decoder; an event with no terminating
   * blank line is discarded, as the specification requires — a half-received
   * event is not an event.
   */
  end(): void {
    this.consume(this.decoder.decode())
    // A CR held back in case an LF followed is, at the end of the stream,
    // simply a line ending.
    if (this.buffer.endsWith('\r')) this.processLine(this.buffer.slice(0, -1))
    this.buffer = ''
    this.dataLines = []
    this.eventType = ''
  }

  private consume(text: string): void {
    const buffer = this.buffer + text
    let lineStart = 0
    let index = 0

    while (index < buffer.length) {
      const char = buffer[index]
      if (char !== '\n' && char !== '\r') {
        index += 1
        continue
      }
      // A CR as the very last character might be the first half of a CRLF
      // whose LF is in the next chunk. Wait rather than guess.
      if (char === '\r' && index === buffer.length - 1) break

      this.processLine(buffer.slice(lineStart, index))
      index += char === '\r' && buffer[index + 1] === '\n' ? 2 : 1
      lineStart = index
    }

    this.buffer = buffer.slice(lineStart)
  }

  private processLine(line: string): void {
    if (line === '') {
      this.dispatch()
      return
    }
    if (line.startsWith(':')) return // a comment: heartbeats land here

    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)

    switch (field) {
      case 'data':
        this.dataLines.push(value)
        break
      case 'event':
        this.eventType = value
        break
      case 'id':
        if (!value.includes('\0')) this.lastEventId = value
        break
      default:
      // `retry` and unknown fields are ignored, per the specification.
    }
  }

  private dispatch(): void {
    if (this.dataLines.length > 0) {
      this.onEvent({
        event: this.eventType || 'message',
        data: this.dataLines.join('\n'),
        id: this.lastEventId,
      })
    }
    this.dataLines = []
    this.eventType = ''
  }
}

const RUN_EVENT_TYPES: ReadonlySet<string> = new Set<RunEvent['type']>([
  'run.started',
  'agent.status',
  'search.started',
  'search.completed',
  'message.delta',
  'citation',
  'message.completed',
  'run.failed',
  'run.cancelled',
])

export const TERMINAL_EVENT_TYPES: ReadonlySet<string> = new Set<RunEvent['type']>([
  'message.completed',
  'run.failed',
  'run.cancelled',
])

/**
 * Decode one event's JSON payload, or null if it is not one we understand.
 *
 * Unknown types are skipped rather than treated as errors, so a server that
 * adds an event type in a later version does not break an older client.
 */
export function parseRunEvent(data: string): RunEvent | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(data)
  } catch {
    return null
  }
  if (typeof parsed !== 'object' || parsed === null || !('type' in parsed)) return null
  const type = parsed.type
  return typeof type === 'string' && RUN_EVENT_TYPES.has(type) ? (parsed as RunEvent) : null
}

/** The stream ended, or broke, before a terminal event arrived. */
export class StreamInterruptedError extends Error {
  constructor(cause?: unknown) {
    super('The connection closed before the answer finished.')
    this.name = 'StreamInterruptedError'
    this.cause = cause
  }
}

export interface RunStreamOptions {
  conversationId: string
  body: RunCreate
  idempotencyKey: string
  onEvent: (event: RunEvent) => void
  signal?: AbortSignal
}

/**
 * Start a run and deliver its events in order until the stream ends.
 *
 * Resolves after a terminal event. Rejects with `ApiError` when the run is
 * refused before streaming (409, 422, 429…), with `StreamInterruptedError`
 * when the connection drops mid-run, and with the abort error when `signal`
 * fires. Aborting is how a client disconnects — and the server cancels a run
 * whose client has gone.
 */
export async function streamRun({
  conversationId,
  body,
  idempotencyKey,
  onEvent,
  signal,
}: RunStreamOptions): Promise<void> {
  const response = await openStream(`/conversations/${conversationId}/runs`, {
    method: 'POST',
    body,
    headers: { 'Idempotency-Key': idempotencyKey },
    ...(signal ? { signal } : {}),
  })
  if (!response.body) throw new NetworkError(new Error('The response had no body.'))

  let finished = false
  const parser = new SSEParser((message) => {
    const event = parseRunEvent(message.data)
    if (event === null) return
    if (TERMINAL_EVENT_TYPES.has(event.type)) finished = true
    onEvent(event)
  })

  const reader = response.body.getReader()
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      parser.push(value)
    }
    parser.end()
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new StreamInterruptedError(cause)
  } finally {
    reader.releaseLock()
  }

  if (!finished) throw new StreamInterruptedError()
}
