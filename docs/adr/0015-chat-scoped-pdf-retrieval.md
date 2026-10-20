# 15. Chat-scoped PDF retrieval (document RAG)

Date: 2026-10-19

## Status

Proposed. Amends `AGENTS.md` §2, which listed file upload and RAG over private
documents as v1 non-goals, and §16, which requires approval for a vector
store. The product owner approved the scope below on 2026-10-19.

## Context

The agent already does retrieval-augmented generation over the web:
`web_search` retrieves, `render_sources_block` fences the excerpts as untrusted,
`synthesize` writes with `[S1]` labels, and `citations.py` drops any label that
names nothing. Users now want to ask questions about their own PDFs.

The product owner fixed four constraints:

1. **A document belongs to exactly one conversation.** There is no per-user
   library, and documents are never shared across chats.
2. **Embeddings come from OpenAI directly**, not from `LLM_BASE_URL`, using the
   cheapest model. OpenAI's pricing page (checked 2026-10-19) lists
   `text-embedding-3-small` at $0.02 per 1M tokens, against $0.10 for
   `text-embedding-ada-002` and $0.13 for `text-embedding-3-large`.
3. **Original files are not stored**, for now. Storing them is a later upgrade.
4. **PDF is the only accepted format.**

Most of the generation half (untrusted fencing, label validation, degraded
notices, eval harness) is reusable. The new risks are elsewhere: hostile files,
one user reading another's text, private text outliving its deletion, and
retrieval returning plausible but irrelevant chunks.

## Decision

### Storage: pgvector in the existing PostgreSQL, exact search, no ANN index

- The `postgres` image becomes `pgvector/pgvector:pg16`, pinned to an exact
  version tag of at least 0.8.2 (0.8.2 fixed a CVE in parallel HNSW builds), in
  dev, prod, and CI. A migration runs `CREATE EXTENSION IF NOT EXISTS vector`.
- **No HNSW or IVFFlat index.** Because a document belongs to one conversation,
  every retrieval is filtered to one conversation, and that conversation holds
  at most `MAX_DOCUMENTS_PER_CONVERSATION × MAX_CHUNKS_PER_DOCUMENT` chunks
  (2,000 with the defaults). A B-tree on `conversation_id` narrows to those rows
  and the database computes exact cosine distance over them. That is fast at
  this size, has perfect recall, and avoids the pgvector pitfall where an
  approximate index applies the `WHERE` filter after the neighbour search and
  returns too few of the caller's own rows. Revisit only if documents ever
  become a cross-conversation library.
- A separate vector database was rejected: ownership could not be enforced in
  the same SQL statement, deletes could not be transactional, and backups,
  migrations, and readiness would each need a second system.

### Embeddings: a provider of their own

- `app/providers/embeddings.py` defines an `EmbeddingProvider` protocol
  (`embed`, `model`, `dimensions`, `max_batch_size`, `aclose`) with one
  implementation over the official `openai` SDK's `AsyncOpenAI`. A single
  `embed` serves documents and queries, because this model embeds both the
  same way. SDK retries are disabled so the retry budget has one owner, as in
  ADR-0007. Errors are classified into transient and permanent at the
  boundary, like the LLM and search providers, with one addition: OpenAI
  answers 429 both for rate limiting and for an exhausted account
  (`insufficient_quota`), and only the first is transient. A missing
  `EMBEDDING_API_KEY` fails before any request is sent.
- `langchain-openai`'s `OpenAIEmbeddings` was rejected: it batches and
  tokenizes on its own terms, which hides the limits this ADR needs to control.
- Configuration is separate from the chat model: `EMBEDDING_API_KEY`,
  `EMBEDDING_BASE_URL` (default `https://api.openai.com/v1`),
  `EMBEDDING_MODEL=text-embedding-3-small`, `EMBEDDING_DIMENSIONS=1536`.
- **Full 1536 dimensions.** The model can return shorter vectors at the same
  price, but per-conversation storage is small, so there is no reason to trade
  away quality. The column is `vector(1536)`. Every document records the
  embedding model and dimensions it was embedded with, and retrieval only
  compares vectors from the configured model.
- The Batch API is not used. It completes within 24 hours, and a user who
  uploads a PDF expects to ask about it within seconds.
- OpenAI caps one request at 8,192 tokens per input and 300,000 tokens across
  all inputs. Chunks stay far below the first limit, and ingestion sends at most
  `EMBEDDING_BATCH_SIZE` (100) chunks per request, roughly 50,000 tokens.
- Cost bound: a maximum-size document (500,000 extracted characters, about
  125,000 tokens) costs about $0.0025 to embed.

### Ingestion: parse in the request, embed in the background, keep no file

1. **Validate.** The upload must be multipart with the `%PDF-` magic bytes; the
   declared `Content-Type` and the extension are not trusted. The size cap
   (`MAX_DOCUMENT_BYTES`, 10 MB) is enforced while the body streams in. Only
   this route gets the larger body limit; everything else keeps 256 KB, in
   nginx and in the API middleware.
2. **Parse in a separate process.** PDF parsing runs in a small
   `ProcessPoolExecutor` (`DOCUMENT_PARSE_WORKERS`, 2) under a timeout
   (`DOCUMENT_PARSE_TIMEOUT_SECONDS`, 30) and an address-space limit set in the
   worker initializer. A hostile PDF can hang or bloat a parser; a process can
   be killed and replaced, and a thread cannot. The process pool also keeps
   CPU-bound parsing off the event loop, which is the same reason ADR-0013
   moved Argon2 into a thread pool. A full pool returns `503`, so callers queue
   on their side rather than in our memory. Parser: `pypdf` (BSD-3-Clause).
   PyMuPDF was rejected for its AGPL licence.
3. **Reject what cannot be answered from.** Encrypted PDFs fail with
   `pdf_encrypted`. PDFs with no text layer (scans) fail with `pdf_no_text`,
   because there is no OCR in this scope. Documents over `MAX_DOCUMENT_PAGES`
   (200) or `MAX_DOCUMENT_CHARS` (500,000) fail with `pdf_too_large`.
4. **Chunk.** Chunks target about 2,000 characters (around 500 tokens), split
   on paragraph and line boundaries, overlap by 200 characters, and never span
   a page boundary, so every chunk cites exactly one page.
5. **Persist once, briefly.** One short transaction inserts the `documents` row
   (status `processing`) and every chunk with a null embedding. The request
   returns `202`. The PDF bytes are discarded when the request ends.
6. **Embed in the background.** An in-process ingestion manager (the same
   pattern as `RunManager`, built in the lifespan) claims a document with an
   `UPDATE … RETURNING` lease using `FOR UPDATE SKIP LOCKED`, embeds its null
   chunks in batches, writes the vectors, and marks the document `ready`.
   A startup and periodic sweep re-claims documents whose lease has expired,
   so a restart resumes embedding from the stored chunk text. Because the text
   is already saved, losing the original file costs nothing here. After
   `DOCUMENT_EMBED_MAX_ATTEMPTS` failures the document is marked `failed` with
   `embedding_unavailable`.

A separate worker service was rejected for now: embedding is network-bound, runs
are already executed in-process, and the sweep makes it restart-safe. Move to a
worker if the ingestion metrics show it slowing streams.

**What not storing the original costs:** changing the chunking strategy or the
parser means asking users to upload again. Re-embedding with a new model does
not, because chunk text is kept. Storing originals in object storage is the
upgrade path.

### Retrieval: hybrid, conversation-scoped, thresholded

- Every chunk query constrains `user_id` **and** `conversation_id` in SQL and
  only reads chunks of `ready` documents embedded with the configured model.
- **Hybrid ranking.** Vector cosine similarity and PostgreSQL full-text search
  (a generated `tsvector` column, GIN-indexed, queried with
  `websearch_to_tsquery`) each produce a ranked list, merged with reciprocal
  rank fusion (k = 60). Full-text search catches exact terms (names, codes,
  section numbers) that embeddings blur.
- **Relevance floor.** A chunk qualifies only if its cosine similarity is at
  least `DOCUMENT_MIN_SIMILARITY` or it matches the full-text query. With no
  qualifying chunk the run behaves like an empty web search: `answer_direct`
  with a notice, never `synthesize` over irrelevant text. The default floor is
  set from the evaluation dataset, not guessed.
- **Budget.** At most `DOCUMENT_TOP_K` (6) chunks and
  `DOCUMENT_CONTEXT_CHARS` (12,000) characters reach the prompt.
- **Query.** When the model-based routing step runs, it also returns a
  standalone `document_query`, so a follow-up such as "what about the second
  one?" is rewritten using the conversation. When a heuristic decides, the
  question is used as written. That is a known weakness, measured by a
  follow-up eval case.
- The query embedding counts against the 90-second run deadline and has its own
  timeout (`DOCUMENT_QUERY_TIMEOUT_SECONDS`, 10), with one retry for transient
  failures. `EMBEDDING_TIMEOUT_SECONDS` (30) bounds each provider request,
  sized for a full batch of chunks during ingestion.

### Graph: one more node, still no cycle

```text
START -> decide_retrieval -> [retrieve_documents] -> [web_search] -> synthesize | answer_direct -> finalize -> END
```

- `decide_search` becomes `decide_retrieval` and sets two independent flags.
  `use_documents` is true when the conversation has a `ready` document and the
  message is not a greeting or a task on text already supplied. Document
  retrieval costs one embedding call (about $0.000001), so it does not wait for
  the model to ask for it; the relevance floor discards irrelevant results.
  `search_decision` keeps its current heuristic-then-model logic.
- Both retrieval nodes are optional, so the longest path is five steps. There
  is still no back edge.
- Labels: web sources stay `S1…`, and document chunks are `D1…`. The prefix
  tells the model, the validator, and the reader what kind of source a claim
  rests on. The citation regexes (backend validator and frontend
  `rehypeCitations.ts`) accept both.
- The prompts extend the untrusted-content clause to documents. A user can
  upload a file written by someone else, so its text is fenced exactly like a
  web page. A new rule tells the model to state disagreement between a document
  and the web. `PROMPT_VERSION` is bumped.

### Citations

`citations` gains `kind` (`web` | `document`), `document_chunk_id`
(`ON DELETE CASCADE`), and `page`. `url` and `display_url` become nullable,
with a check constraint that web citations still have both. The UI renders a
document citation as filename and page with no link. When a document is
deleted, its citation rows go with it, and the `[D1]` marker in the stored
answer renders as plain text, which `rehypeCitations.ts` already does for
unknown labels.

### Deletion and privacy

- **Removing a document** (`DELETE …/documents/{id}`) hard-deletes the document
  and, through cascades, its chunks and citations. It returns `409` while a run
  is active in that conversation, so a run never cites a chunk that has gone.
- **Deleting a conversation** stays a soft delete for conversation and message
  rows, but hard-deletes the conversation's documents in the same transaction.
  Private document text must not survive a delete the user asked for.
- **Checkpoints.** Graph state holds retrieved chunk text, so LangGraph
  checkpoints contain it. Both deletions above call the checkpointer's public
  `adelete_thread(conversation_id)`. This loses nothing, because each run
  rebuilds its transcript from the `messages` table (ADR-0008), and it leaves
  the checkpointer's tables untouched by application SQL (§5). Per-conversation
  scope is what makes this a single call.
- Document text and filenames are never logged. Logs carry `document_id`.
- Deleted text survives in backups until they rotate, and document text plus
  queries are sent to OpenAI for embedding. Both are stated in `security.md`.

### Limits and quotas (all environment-configurable)

| Setting | Default |
| --- | --- |
| `MAX_DOCUMENT_BYTES` | 10 MB |
| `MAX_DOCUMENT_PAGES` | 200 |
| `MAX_DOCUMENT_CHARS` | 500,000 |
| `MAX_CHUNKS_PER_DOCUMENT` | 400 |
| `MAX_DOCUMENTS_PER_CONVERSATION` | 5 |
| `RATE_LIMIT_DOCUMENT_UPLOADS` | 10 per 10 minutes, per user |
| `DAILY_EMBEDDING_TOKEN_LIMIT_PER_USER` | 2,000,000 (about $0.04) |

The same file uploaded twice to one conversation is recognised by its SHA-256
and returns the existing document instead of embedding it again. Uploads
require a verified email, like runs (ADR-0013).

### API

- `POST /conversations/{id}/documents`: multipart upload, returns `202` with
  the document (or `200` with the existing one for a duplicate).
- `GET /conversations/{id}/documents`: not paginated, because it is capped at
  five. This is a documented exception to the cursor-pagination rule.
- `GET /conversations/{id}/documents/{document_id}`: lets the client poll the
  ingestion status.
- `DELETE /conversations/{id}/documents/{document_id}`.

Errors use the existing envelope. New codes: `pdf_invalid`, `pdf_encrypted`,
`pdf_no_text`, `pdf_too_large`, `document_limit_reached`,
`embedding_quota_exceeded`, `document_not_found`, `embedding_unavailable`.
A guessed document or conversation UUID returns `404`.

SSE additions: an `agent.status` value `reading_documents`, a
`documents.retrieved` event carrying document metadata only (never chunk text),
and `kind`, `page`, and `document_id` on the `citation` event.

### Observability and readiness

- Metrics: ingestion duration and failures by code, documents in `processing`,
  embedding requests, tokens and failures, retrieval latency, and the share of
  retrievals with no chunk above the floor.
- **No readiness check for the `vector` extension.** An earlier draft added one.
  It was dropped: the migration creates the extension and fails on a server
  without pgvector, and production runs migrations as a release job before any
  API replica starts, so a ready API without the extension cannot exist.
  Probing for it every few seconds would add load and prove nothing. As with
  the LLM, an embedding-provider outage does not fail readiness or liveness.

## Consequences

- One new runtime dependency set: `pypdf`, `pgvector` (SQLAlchemy type), and
  `python-multipart` (FastAPI multipart parsing). `openai` becomes a direct
  dependency instead of a transitive one. Note that `openai` 3.x uses its own
  HTTP library, `httpx2`, not the `httpx` the rest of the backend uses; only
  the provider's test client is typed with it.
- The PostgreSQL image changes everywhere, including CI services. **An existing
  data volume must not simply be reattached.** The current image is
  `postgres:16-alpine` (musl), and the database was initialised with the libc
  collation `en_US.utf8`. `pgvector/pgvector:pg16` is Debian-based (glibc),
  which sorts text differently, and musl records no collation version that
  would warn about it. Text B-tree indexes, including the unique index on
  `users.email`, could silently misbehave. Existing deployments migrate by
  logical dump and restore with `deploy/backup.sh` and `deploy/restore.sh`,
  which rebuilds every index under the new collation; see
  `docs/deployment.md`, "Moving to the pgvector image". PostgreSQL does not
  warn about this case, because musl records no collation version to compare
  against, so the documents migration checks for it and refuses to run
  (`app/db/migrations_helpers.py`): a database whose recorded collation
  version is NULL while the server reports one was created under musl and
  reattached.
- CI never calls OpenAI. A deterministic fake embedder hashes word tokens into
  a fixed-size vector, so lexically related text scores as similar and
  retrieval tests are meaningful without a model.
- New eval cases: answer in the document, answer absent from the document,
  document and web disagree, prompt injection inside a PDF, a follow-up that
  needs query rewriting, and a second user's document that must never be
  retrieved. The `ci` subset gates citation validity for `D` labels and
  routing.
- Not in scope: OCR, formats other than PDF, tables or images inside PDFs,
  storing original files, a cross-conversation library, rerankers, and
  re-chunking without re-upload.
