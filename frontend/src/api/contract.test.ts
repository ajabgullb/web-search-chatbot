import { inject } from 'vitest'

import { RUN_EVENT_TYPES, TERMINAL_EVENT_TYPES } from '@/api/stream'

/**
 * The committed OpenAPI schema is the contract the generated client comes
 * from. TypeScript checks that every event type the parser lists is real, but
 * not that it lists every one: a type missing here would be dropped silently
 * at runtime. These tests close that gap.
 */
interface RunCreationResponses {
  '200': { content: { 'text/event-stream': { schema: { discriminator: { mapping: object } } } } }
  default: { content: Record<string, unknown> }
}

declare module 'vitest' {
  // Supplied by `test.provide` in vite.config.ts.
  export interface ProvidedContext {
    openapiSchema: string
  }
}

const schema = JSON.parse(inject('openapiSchema')) as {
  paths: {
    '/api/v1/conversations/{conversation_id}/runs': { post: { responses: RunCreationResponses } }
  }
}

const runResponses = schema.paths['/api/v1/conversations/{conversation_id}/runs'].post.responses

describe('API contract', () => {
  it('parses every run event type the server publishes', () => {
    const published = Object.keys(
      runResponses['200'].content['text/event-stream'].schema.discriminator.mapping,
    ).sort()

    expect([...RUN_EVENT_TYPES].sort()).toEqual(published)
  })

  it('treats exactly the three outcome events as terminal', () => {
    expect([...TERMINAL_EVENT_TYPES].sort()).toEqual([
      'message.completed',
      'run.cancelled',
      'run.failed',
    ])
  })

  it('expects run-creation errors as JSON, which is how they arrive', () => {
    expect(Object.keys(runResponses.default.content)).toEqual(['application/json'])
  })
})
