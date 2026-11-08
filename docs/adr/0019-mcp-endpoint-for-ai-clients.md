# 19. An MCP endpoint for users' AI clients, with personal access tokens

Date: 2026-11-07

## Status

Accepted.

## Context

ADR-0017 and ADR-0018 were about coding agents working *on* this repository.
This is the other direction. The product owner asked for users to be able to
reach their own account from an MCP client, such as Claude Code or Cursor:
ask a question and get the cited answer, read past conversations, and search
the PDFs attached to a conversation.

It adds a public, authenticated surface, which AGENTS.md §16 says needs a
decision. Four questions decided its shape.

**Authentication.** The browser holds a 15-minute JWT in memory and a refresh
token in an `HttpOnly` cookie scoped to `/api/v1/auth`. An MCP client can use
neither. The MCP specification's answer for remote servers is OAuth 2.1, with
the API as a protected resource and an authorization server issuing tokens.
That is a large piece of security-critical work: dynamic client registration,
PKCE, consent screens, token endpoints, refresh. Personal access tokens (PATs)
work today with every client that can send a header: Claude Code (`--header`),
Cursor and VS Code (`headers` in their JSON config), and the MCP SDKs.

**Which SDK API.** The Python MCP SDK is at 2.3. `FastMCP` became `MCPServer`,
and protocol revision 2026-07-28 made requests self-contained. The SDK's own
auth support expects an OAuth authorization server: it publishes
protected-resource metadata pointing at one, and on a 401 a client starts an
OAuth flow against it.

**State.** Streamable HTTP can keep sessions in process memory. The API can
run as several replicas behind a load balancer with no sticky sessions, and
everything else it does (runs, locks, rate limits) is coordinated through
PostgreSQL and Redis rather than memory.

**What the tools should be.** The REST services already do everything a tool
needs, with ownership checked in every query, and the route handlers are thin
adapters over them. An MCP tool can be one more thin adapter.

## Decision

- **Personal access tokens, not OAuth, for now.** A user creates one per
  client on a new "AI clients" page (`/settings/ai-clients`). Each token:
  - is `wsc_pat_` plus 32 random bytes. The prefix lets the endpoint refuse
    anything else before it touches the database, and lets secret scanners
    recognise a leaked token;
  - is stored only as a SHA-256 hash, like refresh tokens;
  - has a name, one or both scopes (`conversations:read`, `runs:create`), and
    an expiry of at most 90 days (`ACCESS_TOKEN_MAX_TTL_DAYS`);
  - can be revoked at once, and all of them are revoked by a password reset;
  - is capped at 10 live per account (`MAX_ACCESS_TOKENS_PER_USER`);
  - works only at the MCP endpoint. The REST API accepts only the browser
    JWT, so a leaked PAT can never mint, list, or revoke tokens.
- **The SDK's transport, our authentication.** `MCPServer` provides the
  tools and the Streamable HTTP transport. A small ASGI endpoint in front of
  it does the checks, in this order: `Origin` (required by the MCP
  specification, refused with 403 if present and not the frontend's), the
  bearer token (401 with `WWW-Authenticate: Bearer realm="mcp"`, the same for
  every way a token can be wrong), then rate limits. The SDK's OAuth support
  is not used, so the server never advertises an authorization server that
  does not exist.
- **Stateless, at `/api/v1/mcp`.** Each request stands alone and is
  authenticated by itself, so any replica can answer it. It sits under
  `/api/` so the existing nginx location proxies it unchanged, with
  buffering off for server-sent events. The tools read the token from the
  ASGI scope of the HTTP request that carried the message, never from
  connection state.
- **Four tools and one resource, all over existing services:**

  | Name | Scope | Service |
  |---|---|---|
  | `ask(question, conversation_id?)` | `runs:create` (+ `conversations:read` to continue a conversation) | `RunService.create` and `RunManager.start`, exactly as `POST .../runs` |
  | `list_conversations(cursor?, limit?)` | `conversations:read` | `ConversationService.list` |
  | `get_messages(conversation_id, before?, limit?)` | `conversations:read` | `ConversationService.list_messages` |
  | `search_documents(conversation_id, query)` | `conversations:read` | `PostgresDocumentRetriever.search` |
  | `conversation://{id}` (resource) | `conversations:read` | `ConversationService.list_messages`, rendered as Markdown |

  So `ask` gets every rule a browser run gets: email verification, the
  per-minute run limit (the same per-user bucket), the daily allowance, one
  active run per conversation, persistence, and metrics. A run started over
  MCP is an ordinary run, and the conversation appears in the app.
- **Progress and cancellation.** `ask` relays the run's status, search, and
  document events as MCP progress notifications, so it needs the SSE response
  mode. If the client goes away, the SDK cancels the tool call, and the tool
  cancels the run at once (`client_disconnected`). There is no grace period,
  because an MCP client has no recovery endpoint to come back through.
- **Untrusted text is labelled in the result.** Snippets from web pages and
  passages from PDFs reach the client's model, which may hold tools far more
  powerful than ours. They go in fields named `untrusted_excerpt` and
  `untrusted_text`, and the server's `instructions` tell the model that
  anything in an `untrusted_` field is data and cannot give instructions. It
  is the same rule as the agent's own source fencing, in a form that survives
  JSON.
- **Limits.** 120 MCP requests per token per minute
  (`RATE_LIMIT_MCP_REQUESTS`), 30 failed token checks per client IP per
  5 minutes (`RATE_LIMIT_MCP_AUTH_FAILURES`), and `search_documents` counts
  against the daily embedding allowance, because every search embeds the
  query. `MCP_ENABLED=false` turns the endpoint off (404) without touching
  stored tokens.
- **Privacy in logs.** One `mcp_tool_call` line per call, with the tool name,
  outcome, and duration, never its arguments or result. The SDK and
  `sse-starlette` log whole messages at DEBUG, so both are held at WARNING.
  `wsc_pat_…` strings are redacted wherever they appear. `test_log_hygiene`
  covers an MCP session.

## Consequences

- Users can reach the assistant from their own AI tools, and every question
  they ask that way is recorded in the conversation history. The app's
  quotas cover it with no new budget to tune.
- A client that can connect only through OAuth, with no way to send a
  custom header, cannot use this yet. An OAuth authorization server added
  later would sit beside PATs, not replace them, and would reuse the same
  endpoint and tools.
- A PAT is a long-lived bearer secret in a config file on the user's
  machine. The expiry, the scopes, revocation, `last_used_at`, and the
  password-reset revocation limit the damage. There is no per-token IP
  allowlist.
- Stateless mode means no server-initiated requests (sampling, elicitation)
  and no resumable streams. None of the tools needs them. Cancelling a call
  works by closing its HTTP request: a legacy `notifications/cancelled`
  message arrives on a different request, where there is nothing to cancel.
- New metrics: `mcp_endpoint_tool_calls_total{tool,outcome}`,
  `mcp_endpoint_tool_duration_seconds{tool}`, and
  `mcp_endpoint_refusals_total{reason}`. They are named `mcp_endpoint_*`
  because ADR-0018's development dashboard already uses `mcp_*` series.
- The SDK's `Context.request_context.request` is how a tool finds its
  principal, and the SDK marks that attribute for rework. The tools fail
  closed if it ever stops carrying the request. The integration tests would
  catch that on an SDK upgrade before users did.
