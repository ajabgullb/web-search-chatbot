/**
 * A server-sent event stream a test controls event by event.
 *
 * The test pushes events and asserts on the UI between them, which is the
 * only way to observe incremental rendering rather than just the end state.
 */

const encoder = new TextEncoder()

export function frame(event: { sequence: number; type: string }): string {
  return `id: ${event.sequence}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`
}

export interface ControlledStream {
  response: Response
  /** Deliver one event, optionally cut into small byte chunks. */
  send: (event: { sequence: number; type: string }, chunkSize?: number) => void
  heartbeat: () => void
  close: () => void
}

export function controlledStream(signal?: AbortSignal | null): ControlledStream {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({
    start(streamController) {
      controller = streamController
    },
  })
  // A real fetch rejects its body reads when aborted; mirror that.
  signal?.addEventListener('abort', () => {
    try {
      controller.error(new DOMException('The operation was aborted.', 'AbortError'))
    } catch {
      // already closed
    }
  })

  return {
    response: new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }),
    send(event, chunkSize) {
      const bytes = encoder.encode(frame(event))
      const size = chunkSize ?? bytes.length
      for (let at = 0; at < bytes.length; at += size) {
        controller.enqueue(bytes.slice(at, at + size))
      }
    },
    heartbeat() {
      controller.enqueue(encoder.encode(': heartbeat\n\n'))
    },
    close() {
      controller.close()
    },
  }
}

/** Builds a run's events with consecutive sequence numbers. */
export function eventsFor(runId: string) {
  let sequence = 0
  return <T extends Record<string, unknown>>(type: string, fields: T = {} as T) => ({
    sequence: ++sequence,
    run_id: runId,
    type,
    ...fields,
  })
}
