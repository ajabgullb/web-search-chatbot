# 5. Cursor pagination and soft deletion for conversations

Date: 2026-09-06

## Status

Accepted.

## Context

The sidebar lists conversations newest-first while the user is creating and
renaming them, and threads can be long enough to page. Two decisions were open:
how to paginate, and what "delete" means.

`AGENTS.md` §8 already requires cursor pagination rather than offset, and §5
requires a `deleted_at` column. This record captures *why*, and the shape the
implementation settled on.

## Decision

**Keyset cursors, not OFFSET.** A page is fetched with
`WHERE (updated_at, id) < (:cursor_updated_at, :cursor_id)` ordered by
`(updated_at DESC, id DESC)`. The cursor is a base64url encoding of that pair.

The sort key is `updated_at`, which every write bumps — so the list reorders
constantly while it is being read. Under OFFSET, a conversation created or
renamed above the current page pushes everything down and page two repeats the
last row of page one. A cursor names the last row *seen*, so concurrent writes
are simply invisible to a walk in progress.

`id` is the tiebreaker: two conversations can share a timestamp, and without a
total order the comparison is not a stable boundary.

The encoding is not encryption and hides nothing secret. It exists so clients
treat the cursor as opaque and do not freeze the sort key into their own code.
A cursor that fails to decode is a `400`, never a silent fall back to page one —
falling back would loop a paginating client forever.

Messages use the same mechanism with a single-field cursor (`sequence_number`)
under the name `before`, because chat history is read from the newest turn
backwards.

**Soft deletion.** `DELETE /conversations/{id}` stamps `deleted_at`. Every read
filters `deleted_at IS NULL`, so a deleted conversation vanishes from listings
and returns `404` on direct access — but the row and its messages survive for
audit, and the run history Phase 6 adds keeps a valid parent.

A partial index, `(user_id, updated_at DESC, id DESC) WHERE deleted_at IS NULL`,
matches the listing query exactly. `EXPLAIN` confirms an index scan with no sort
and no post-filter.

## Consequences

- Clients cannot jump to "page 5". Nothing in the product asks to.
- A total row count is not available without a separate query. Nothing needs one.
- Soft-deleted rows accumulate. A purge job is a later concern and is noted in
  `docs/security.md` under data retention.
- `updated_at` as the sort key means renaming moves a conversation to the top.
  That matches how every comparable product behaves.
