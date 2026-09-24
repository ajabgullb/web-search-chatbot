# 8. Runs and streaming: a task per run, the database as coordinator

Date: 2026-09-23

## Status

Accepted. Disconnect handling amended by
[ADR-0009](0009-streaming-frontend.md): a dropped stream starts a short
observation lease instead of cancelling at once. Extended by
[ADR-0010](0010-redis-run-lock.md): an application-level run lock now sits in
front of the one-active-run index, which remains the source of truth.

## Context

`AGENTS.md` §8 and `PLAN.md` Phase 6 require the agent to be exposed as an SSE
stream with idempotent creation, one active run per conversation, cooperative
cancellation, a recovery endpoint, and two guarantees that pull in opposite
directions: a client that disconnects must stop server-side work, yet an answer
the model has finished must be persisted even if the stream dies. Redis — the
contract's tool for cross-replica coordination — is a Phase 8 dependency and
must not be required yet.

## Decision

**The run executes in its own asyncio task, not in the HTTP response.** The
response and the run have different lifetimes. The task publishes numbered
events to a per-run queue; the SSE generator only relays them. When the
generator is torn down early — Starlette cancels it on `http.disconnect` — its
`finally` cancels the task, which interrupts the model stream at its next await.
When the model has already finished, the outcome is written under
`asyncio.shield`, so the cancellation cannot interrupt the save.

**The database coordinates, not process memory.** One active run per
conversation is a partial unique index on `runs(conversation_id) WHERE status IN
('queued','running')`. Idempotency is `UNIQUE (user_id, idempotency_key)` plus a
SHA-256 fingerprint of the normalized request. Every transition is a conditional
`UPDATE … WHERE status IN (active)` that reports whether it won. Concurrency is
therefore correct under any interleaving and across replicas today; the
in-process registry of tasks is only an accelerator for local cancellation.
Phase 8's Redis adds prompt cross-replica cancellation and distributed rate
limits without changing these invariants.

**The user message and the run commit together.** A request that loses a race
on any unique index rolls back both, so a `409` leaves nothing behind. After a
unique violation the service re-checks the idempotency key first: a violation
caused by the same request sent twice is a replay, not a conflict.

**Idempotent replay is a short stream, not a different content type.** A
repeated key for a finished run returns `run.started` (`replayed: true`) plus the
original terminal event, with `Idempotent-Replayed: true`. Clients parse one
format. A repeated key for a still-active run returns `409` with the run id — as
an idempotent API does for a request still being processed — rather than
multiplexing a second subscriber onto the live queue.

**The `messages` table is the canonical history; the checkpoint mirrors it.**
Each run loads its history from the database and the graph input begins with
`RemoveMessage(REMOVE_ALL_MESSAGES)`, replacing the checkpoint's transcript.
Messages carry their database ids, so the two histories name the same turns.
This fixes the drift noted after Phase 5, where a failed run left an orphaned
question in the checkpoint that the product did not show, and it makes retry
trivial: a retry re-runs the same stored question, never a copy of it.

**Cancelled and failed runs save no assistant message.** A stopped response is
not an answer, and saving partial text would need a retry to hide or replace it.
The question stays; `retry_message_id` answers it again. The streamed partial
text remains on the client's screen until reload.

**Streamed text is validated as it streams.** `CitationStreamFilter` holds back
only a trailing fragment that could still become a marker (at most 48
characters) and applies the same rule as the batch validator, so an invented
`[S9]` never appears on screen. A `citation` event precedes the first delta
containing its marker. The saved answer additionally tidies whitespace, so
`message.completed` carries the canonical text and clients replace what they
concatenated.

**Plain `StreamingResponse`, not FastAPI's `EventSourceResponse`.** The built-in
support streams from a generator path operation, which commits the `200` before
the handler runs; our 404/409/422 outcomes must be ordinary JSON errors. The
heartbeat interval is configuration rather than a library constant. The event
union is attached to the OpenAPI `text/event-stream` response explicitly, since
FastAPI describes non-JSON media as a string, so the generated TypeScript client
has every event type.

**Crashed runs expire.** A run older than `RUN_TIMEOUT_SECONDS +
RUN_STALE_GRACE_SECONDS` cannot be alive, so it is failed as `run_interrupted`
when its conversation next receives a message or the run is polled. Without this
a crash would lock the conversation forever behind the unique index.

## Consequences

- A killed client stops work within one await; the model provider may still
  bill for tokens generated before the abort.
- With several replicas, a user's cancel reaches a run on another replica only
  through the database, so that replica stops at the end of the model call
  rather than immediately. Phase 8 closed this gap with a Redis pub/sub cancel
  signal; the database stays the durable record.
- The per-user run rate limit was in-process until Phase 8 moved it to Redis.
- Resumable streams (`Last-Event-ID`) are not supported; recovery is
  `GET /runs/{id}` plus message history, as §8 allows.
- `LLMProvider` exposes `stream` and `structured`; `complete` was removed rather
  than kept unused. Real-provider streaming asks for usage on the final chunk
  (`stream_usage`), which OpenAI, OpenRouter, Groq, and Ollama support.
- Clean shutdown fails in-flight runs as retryable; draining them is Phase 8.
