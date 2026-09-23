# 9. Streaming frontend, and watching a run as a lease

Date: 2026-09-24

## Status

Accepted. Amends the disconnect handling of
[ADR-0008](0008-runs-and-streaming.md).

## Context

`PLAN.md` Phase 7 asks the frontend to render the run stream incrementally,
reconcile it with the persisted message, stop and retry, follow the answer
without fighting the reader, and — explicitly — recover the run state and final
message when the page is reloaded mid-stream.

That last requirement collided with Phase 6. Phase 6's definition of done says a
killed client connection stops server-side work, and ADR-0008 implemented it
literally: the stream closing cancelled the run. A reload *is* a closed
connection, so a reloaded page found its answer already cancelled. The first
real-browser run of the Phase 7 checks failed exactly there.

## Decision

**Watching a run is a lease, not a connection.** When a run's stream closes,
the run is not cancelled at once. It gets `RUN_ABANDON_GRACE_SECONDS` (default
10 s). Every `GET /runs/{id}` from its owner renews the lease; a run nobody
renews within the grace period is cancelled as `client_disconnected`. A reload
polls within a second or two and keeps its answer; a closed tab never polls and
stops spending ten seconds later. Both definitions of done hold, the first
bounded by the grace period. The renewing poll is ownership-checked before it
counts, so a stranger cannot keep someone else's run alive.

**`GET /conversations/{id}` reports `active_run_id`.** A freshly loaded page has
no stream and no run id; without this field it could not know there is a run to
follow. It is on the single-conversation read only, so the sidebar listing
stays one query.

**`fetch` and a hand-written SSE parser, not `EventSource`.** `EventSource`
cannot POST and cannot send an `Authorization` header. The parser follows the
WHATWG rules and is tested against every two-way split of a stream and one byte
at a time, including splits inside multi-byte characters and CRLF pairs.

**Live runs live in a store outside React.** A plain class, read through
`useSyncExternalStore`, keyed by conversation. An answer keeps streaming while
the user switches threads, and all event handling is ordinary methods rather
than effects. Snapshots are immutable, so a component re-renders only when its
own conversation's run changes. Leaving the chat (signing out) hangs up every
stream and clears every entry, so nothing keeps polling — and therefore
nothing keeps renewing a lease — after the page is gone.

**Canonical replaces streamed in one update.** `message.completed` merges the
server's message into the TanStack Query cache and clears the streamed text in
the same synchronous turn. The thread can never show the answer twice, or
neither copy for a frame.

**Retry uses idempotency in two ways.** After a failed or stopped answer, retry
sends `retry_message_id` under a new key: the question is answered again, never
stored twice. After a send that was refused or may never have arrived, retry
repeats the exact request under the *same* key: if the first attempt did land,
the server replays it.

**The composer is limited, not disabled, while an answer streams.** The next
message can be drafted; only sending is blocked, and the send button becomes
Stop. Disabling the textarea would throw a keyboard user's focus out of it.

**Scroll following is decided by scroll events.** Measuring the distance to the
bottom after each render misreads a large chunk of new text as the reader having
scrolled away. Scroll events come only from the reader or our own
scroll-to-bottom, so only the reader can turn following off. A `ResizeObserver`
catches growth `followKey` cannot see: deferred renders of the streamed text,
and the lazy Markdown chunk replacing its plain-text fallback. The real-browser
check caught the deferred-render case, which jsdom cannot show.

## Consequences

- A tab closed mid-answer costs up to `RUN_ABANDON_GRACE_SECONDS` of generation.
- A stopped answer's partial text stays on screen until reload; it is not saved.
- Phase 9's Playwright suite should keep the scenarios verified by hand here:
  stream, cite, stop, retry, reload mid-stream, scroll up while streaming, and
  a phone-width layout.
- The Markdown chunk (~100 kB gzipped, mostly the syntax highlighter) loads
  with the first answer; until then an answer shows as plain text, briefly
  including raw `[S1]` markers.
