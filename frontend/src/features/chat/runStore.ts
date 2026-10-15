/**
 * Live runs: what is streaming right now, per conversation.
 *
 * A plain class rather than React state, for two reasons. A run must outlive
 * the page that started it — switching to another conversation mid-answer
 * should not kill the answer — so its state cannot live in a component that
 * unmounts. And the event handling is easier to reason about (and to test)
 * as ordinary methods than as effects.
 *
 * React reads it through `useSyncExternalStore`. Each conversation's entry is
 * an immutable snapshot replaced on every change, so a component re-renders
 * exactly when its own conversation's run changes.
 *
 * The lifecycle of one entry:
 *
 *   sending ──run.started──▶ streaming ──message.completed──▶ completed
 *      │                        │  ├───run.failed────────────▶ failed
 *      │                        │  └───run.cancelled─────────▶ cancelled
 *      │                        └──connection lost──▶ recovering ──▶ (any end state)
 *      └──refused / never reached the server──▶ rejected
 *
 * The streamed text is shown only until the server's canonical message is in
 * the TanStack Query cache; both happen in the same update, so the thread
 * never shows the answer twice or not at all.
 */

import type { QueryClient } from '@tanstack/react-query'

import { ApiError, NetworkError } from '@/api/errors'
import { StreamInterruptedError, streamRun } from '@/api/stream'
import type {
  AgentStatus,
  ChatMessage,
  Citation,
  Run,
  RunCreate,
  RunEvent,
  Source,
} from '@/api/types'
import { mergeMessages, refreshConversation } from '@/features/chat/cache'
import * as chatApi from '@/features/chat/api'
import { chatKeys } from '@/features/chat/queries'
import { randomId } from '@/lib/ids'

export type LivePhase =
  'sending' | 'streaming' | 'recovering' | 'completed' | 'failed' | 'cancelled' | 'rejected'

export interface LiveSearch {
  query: string
  sources: Source[]
  done: boolean
  failed: boolean
}

export interface LiveError {
  code: string
  message: string
  retryable: boolean
}

export interface LiveRun {
  conversationId: string
  phase: LivePhase
  /** The text the user sent, shown optimistically until the server confirms it. */
  pendingContent: string | null
  /** The stored user message's id, once `run.started` has confirmed it. */
  userMessageId: string | null
  runId: string | null
  status: AgentStatus | null
  searches: LiveSearch[]
  text: string
  citations: Citation[]
  error: LiveError | null
  cancelReason: string | null
  /** True between asking the server to stop and hearing that it has. */
  stopping: boolean
  /** One sentence for the screen-reader live region. Changes per phase, not per token. */
  announcement: string
  /** The exact request, so a send that never arrived can be repeated safely. */
  request: { body: RunCreate; idempotencyKey: string } | null
}

export const ACTIVE_PHASES: ReadonlySet<LivePhase> = new Set(['sending', 'streaming', 'recovering'])

export function isActive(run: LiveRun | null | undefined): boolean {
  return run != null && ACTIVE_PHASES.has(run.phase)
}

/** Test seam: polling is slow in real life and must be fast in a test. */
export const runStoreTiming = {
  pollIntervalMs: 1_000,
  // The run deadline is 90 s and a dead run expires a minute after that, so a
  // recovery that has heard nothing definitive for three minutes gives up.
  recoveryLimitMs: 180_000,
}

const STATUS_ANNOUNCEMENTS: Record<AgentStatus, string> = {
  thinking: 'The assistant is thinking.',
  searching: 'Searching the web.',
  writing: 'Writing the answer.',
}

function blankRun(conversationId: string, overrides: Partial<LiveRun>): LiveRun {
  return {
    conversationId,
    phase: 'sending',
    pendingContent: null,
    userMessageId: null,
    runId: null,
    status: null,
    searches: [],
    text: '',
    citations: [],
    error: null,
    cancelReason: null,
    stopping: false,
    announcement: '',
    request: null,
    ...overrides,
  }
}

function describeFailure(error: unknown): LiveError {
  if (error instanceof ApiError) {
    if (error.is('run_in_progress')) {
      return {
        code: error.code,
        message: 'This conversation is still answering a previous message.',
        retryable: true,
      }
    }
    if (error.is('email_not_verified')) {
      return {
        code: error.code,
        message: 'Confirm your email address to start sending messages. Check your inbox.',
        retryable: false,
      }
    }
    if (error.is('daily_quota_exceeded')) {
      const seconds = Number(error.details?.retry_after_seconds)
      const hours = Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds / 3600) : null
      const wait = hours === null ? '' : ` You can send more in about ${hours} h.`
      return {
        code: error.code,
        message: `You have reached your daily message limit.${wait}`,
        retryable: false,
      }
    }
    if (error.is('rate_limited')) {
      const seconds = Number(error.details?.retry_after_seconds)
      const wait = Number.isFinite(seconds) && seconds > 0 ? ` Try again in ${seconds}s.` : ''
      return {
        code: error.code,
        message: `You are sending messages too quickly.${wait}`,
        retryable: true,
      }
    }
    return { code: error.code, message: error.message, retryable: error.status >= 500 }
  }
  if (error instanceof NetworkError || error instanceof StreamInterruptedError) {
    return { code: 'network_error', message: error.message, retryable: true }
  }
  return { code: 'unknown_error', message: 'Something went wrong. Try again.', retryable: true }
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

type Listener = () => void

export class RunStore {
  private readonly queryClient: QueryClient
  private readonly runs = new Map<string, LiveRun>()
  private readonly controllers = new Map<string, AbortController>()
  private readonly listeners = new Set<Listener>()

  constructor(queryClient: QueryClient) {
    this.queryClient = queryClient
  }

  // --- external store plumbing -------------------------------------------

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  get(conversationId: string): LiveRun | null {
    return this.runs.get(conversationId) ?? null
  }

  private set(conversationId: string, next: LiveRun): void {
    this.runs.set(conversationId, next)
    for (const listener of this.listeners) listener()
  }

  private update(conversationId: string, change: Partial<LiveRun>): LiveRun | null {
    const current = this.runs.get(conversationId)
    if (!current) return null
    const next = { ...current, ...change }
    this.set(conversationId, next)
    return next
  }

  // --- actions ------------------------------------------------------------

  /** Send a new message. Ignored while this conversation is already answering. */
  send(conversationId: string, content: string): void {
    if (isActive(this.get(conversationId))) return
    const request = { body: { content }, idempotencyKey: randomId() }
    this.set(
      conversationId,
      blankRun(conversationId, {
        pendingContent: content,
        request,
        announcement: 'Sending your message.',
      }),
    )
    void this.stream(conversationId, request)
  }

  /**
   * Try again.
   *
   * After a failed or stopped answer, the stored question is answered again
   * (`retry_message_id`) under a fresh key: the question is never duplicated.
   * After a send that was refused or never arrived, the *same* request is
   * repeated with the *same* key — if the first attempt did reach the server
   * after all, the server replays it instead of creating a second run.
   */
  retry(conversationId: string, messageId?: string): void {
    const current = this.get(conversationId)
    if (isActive(current)) return

    if (current?.phase === 'rejected' && current.request) {
      const request = current.request
      this.set(
        conversationId,
        blankRun(conversationId, {
          pendingContent: current.pendingContent,
          request,
          announcement: 'Sending your message again.',
        }),
      )
      void this.stream(conversationId, request)
      return
    }

    const retryMessageId = messageId ?? current?.userMessageId
    if (!retryMessageId) return
    const request = { body: { retry_message_id: retryMessageId }, idempotencyKey: randomId() }
    this.set(
      conversationId,
      blankRun(conversationId, {
        userMessageId: retryMessageId,
        request,
        announcement: 'Trying again.',
      }),
    )
    void this.stream(conversationId, request)
  }

  /**
   * Stop the answer.
   *
   * Once the run exists, stopping asks the *server* to cancel it — hiding the
   * stream locally would leave the model running and billing. Before the run
   * exists there is nothing to name, so the request is aborted instead; the
   * server treats a vanished client as a cancellation.
   */
  async stop(conversationId: string): Promise<void> {
    const current = this.get(conversationId)
    if (!current || !isActive(current) || current.stopping) return

    if (current.runId === null) {
      this.controllers.get(conversationId)?.abort()
      return
    }

    this.update(conversationId, { stopping: true, announcement: 'Stopping the answer.' })
    try {
      const run = await chatApi.cancelRun(current.runId)
      // A live stream will now deliver run.cancelled on its own. A recovered
      // run has no stream, so its outcome is applied from the response.
      if (this.get(conversationId)?.phase === 'recovering') this.finishFromRun(conversationId, run)
    } catch {
      // The cancel request itself failed. Hanging up still stops the run —
      // but only if nothing keeps watching it: polling for recovery would
      // renew the server's observation lease and keep it alive. So the entry
      // is ended here, before the abort lands and could trigger recovery.
      this.update(conversationId, {
        phase: 'cancelled',
        cancelReason: 'user_cancelled',
        stopping: false,
        announcement: 'Answer stopped.',
      })
      this.controllers.get(conversationId)?.abort()
    }
  }

  /**
   * Follow a run this tab has no stream for — after a reload, or after the
   * connection dropped mid-answer — by polling its recovery endpoint.
   * Idempotent: asking to recover the run already being recovered is a no-op.
   */
  recover(conversationId: string, runId: string): void {
    const current = this.get(conversationId)
    if (current && current.runId === runId && isActive(current)) return
    this.set(
      conversationId,
      blankRun(conversationId, {
        ...(current?.runId === runId ? current : {}),
        phase: 'recovering',
        runId,
        stopping: false,
        announcement: 'Reconnecting to the answer in progress.',
      }),
    )
    void this.poll(conversationId, runId)
  }

  /** Drop a finished entry, e.g. once its outcome has been acknowledged. */
  dismiss(conversationId: string): void {
    if (isActive(this.get(conversationId))) return
    this.runs.delete(conversationId)
    for (const listener of this.listeners) listener()
  }

  /**
   * Hang up on every stream and forget every run: signing out, or leaving the
   * chat entirely. Entries are dropped *before* the aborts land, so the
   * aborted streams find nothing to recover and no polling outlives the page.
   */
  abortAll(): void {
    const controllers = [...this.controllers.values()]
    this.controllers.clear()
    this.runs.clear()
    for (const controller of controllers) controller.abort()
    for (const listener of this.listeners) listener()
  }

  // --- streaming ----------------------------------------------------------

  private async stream(
    conversationId: string,
    request: { body: RunCreate; idempotencyKey: string },
  ): Promise<void> {
    const controller = new AbortController()
    this.controllers.set(conversationId, controller)

    try {
      await streamRun({
        conversationId,
        body: request.body,
        idempotencyKey: request.idempotencyKey,
        signal: controller.signal,
        onEvent: (event) => this.apply(conversationId, event),
      })
    } catch (error) {
      this.handleStreamError(conversationId, error)
    } finally {
      if (this.controllers.get(conversationId) === controller) {
        this.controllers.delete(conversationId)
      }
    }
  }

  private handleStreamError(conversationId: string, error: unknown): void {
    const current = this.get(conversationId)
    if (!current || !isActive(current)) return

    if (current.runId !== null) {
      // The run exists on the server and carries on without us (unless we
      // aborted, in which case the server cancels it). Either way its outcome
      // is recorded there, so ask rather than guess.
      this.recover(conversationId, current.runId)
      return
    }

    if (isAbort(error)) {
      this.update(conversationId, {
        phase: 'cancelled',
        cancelReason: 'user_cancelled',
        announcement: 'Answer stopped.',
      })
      // The question may have been stored before the hang-up; let the
      // history show it (with a retry) if so.
      void this.queryClient.invalidateQueries({ queryKey: chatKeys.messages(conversationId) })
      return
    }

    const failure = describeFailure(error)
    this.update(conversationId, {
      phase: 'rejected',
      error: failure,
      announcement: `Message not sent. ${failure.message}`,
    })
  }

  /** Apply one stream event. The only place stream events change state. */
  private apply(conversationId: string, event: RunEvent): void {
    const current = this.get(conversationId)
    if (!current) return

    switch (event.type) {
      case 'run.started':
        mergeMessages(this.queryClient, conversationId, [event.user_message])
        this.update(conversationId, {
          phase: 'streaming',
          runId: event.run_id,
          userMessageId: event.user_message.id,
          pendingContent: null,
        })
        refreshConversation(this.queryClient, conversationId)
        return

      case 'agent.status':
        this.update(conversationId, {
          status: event.status,
          announcement: STATUS_ANNOUNCEMENTS[event.status],
        })
        return

      case 'search.started':
        this.update(conversationId, {
          searches: [
            ...current.searches,
            { query: event.query, sources: [], done: false, failed: false },
          ],
        })
        return

      case 'search.completed': {
        const searches = current.searches.map((search) =>
          search.query === event.query && !search.done
            ? { ...search, sources: event.sources, done: true, failed: event.failed ?? false }
            : search,
        )
        this.update(conversationId, { searches })
        return
      }

      case 'citation':
        // Arrives before the text that uses it, so a marker is never shown
        // without its source.
        this.update(conversationId, { citations: [...current.citations, event.citation] })
        return

      case 'message.delta':
        this.update(conversationId, { text: current.text + event.text })
        return

      case 'message.completed':
        this.complete(conversationId, event.message)
        return

      case 'run.failed':
        this.update(conversationId, {
          phase: 'failed',
          error: event.error,
          stopping: false,
          announcement: `The answer failed. ${event.error.message}`,
        })
        return

      case 'run.cancelled':
        this.update(conversationId, {
          phase: 'cancelled',
          cancelReason: event.reason,
          stopping: false,
          announcement: 'Answer stopped.',
        })
        return
    }
  }

  private complete(conversationId: string, message: ChatMessage): void {
    // Canonical message into the cache and the streamed copy out, in the same
    // synchronous turn: React renders one of them, never both.
    mergeMessages(this.queryClient, conversationId, [message])
    this.update(conversationId, {
      phase: 'completed',
      text: '',
      citations: [],
      searches: [],
      status: null,
      stopping: false,
      announcement: message.degraded
        ? 'Answer complete. It could not be verified against sources.'
        : 'Answer complete.',
    })
    refreshConversation(this.queryClient, conversationId)
  }

  // --- recovery -------------------------------------------------------------

  private async poll(conversationId: string, runId: string): Promise<void> {
    const deadline = Date.now() + runStoreTiming.recoveryLimitMs
    const stillRecovering = () => {
      const current = this.get(conversationId)
      return current?.phase === 'recovering' && current.runId === runId
    }

    while (stillRecovering()) {
      try {
        const run = await chatApi.getRun(runId)
        if (!stillRecovering()) return
        if (run.status !== 'queued' && run.status !== 'running') {
          this.finishFromRun(conversationId, run)
          return
        }
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          // The run is gone with its conversation; nothing to show.
          this.runs.delete(conversationId)
          for (const listener of this.listeners) listener()
          return
        }
        // Anything else is transient: keep polling until the deadline.
      }

      if (Date.now() > deadline) {
        this.update(conversationId, {
          phase: 'failed',
          error: {
            code: 'recovery_timeout',
            message: 'Lost track of the answer. Reload to see whether it finished.',
            retryable: false,
          },
          announcement: 'Lost track of the answer.',
        })
        return
      }
      await sleep(runStoreTiming.pollIntervalMs)
    }
  }

  private finishFromRun(conversationId: string, run: Run): void {
    if (run.status === 'completed' && run.assistant_message) {
      this.complete(conversationId, run.assistant_message)
      return
    }
    if (run.status === 'cancelled') {
      this.update(conversationId, {
        phase: 'cancelled',
        cancelReason: run.cancel_reason ?? 'user_cancelled',
        userMessageId: run.user_message_id,
        stopping: false,
        announcement: 'Answer stopped.',
      })
    } else {
      const error = run.error ?? {
        code: 'internal_error',
        message: 'The answer could not be completed.',
        retryable: true,
      }
      this.update(conversationId, {
        phase: 'failed',
        error,
        userMessageId: run.user_message_id,
        stopping: false,
        announcement: `The answer failed. ${error.message}`,
      })
    }
    // Whatever happened, the history may now hold messages this tab never saw.
    void this.queryClient.invalidateQueries({ queryKey: chatKeys.messages(conversationId) })
    refreshConversation(this.queryClient, conversationId)
  }
}
