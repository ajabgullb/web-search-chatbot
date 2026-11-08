# Using the assistant from an AI client (MCP)

The app is also a [Model Context Protocol](https://modelcontextprotocol.io)
server. Connect Claude Code, Cursor, VS Code, or any MCP client that can talk
to a remote server, and that client can ask your assistant questions, read
your conversations, and search the PDFs attached to them, all as you.
[ADR-0019](adr/0019-mcp-endpoint-for-ai-clients.md) records the design.

This is a product feature. [mcp.md](mcp.md) is about something else: the
development MCP servers that coding agents use to work on this repository.

## Connect a client

1. Sign in, open the sidebar, and choose the plug icon, **AI clients**
   (`/settings/ai-clients`).
2. Create a token. Name it after the client it's for, choose what it may do,
   and choose how long it lasts (7, 30, or 90 days).
3. Copy what the page shows straight away. The token is never shown again.

The server URL is the app's own origin plus `/api/v1/mcp`, for example
`https://chat.example.com/api/v1/mcp`. In development it's
`http://localhost:5173/api/v1/mcp`; Vite proxies it to the API.

**Claude Code:**

```bash
claude mcp add --transport http web-search-chatbot https://chat.example.com/api/v1/mcp \
  --header "Authorization: Bearer wsc_pat_..."
```

**Cursor, VS Code, and other clients with a JSON configuration:**

```json
{
  "mcpServers": {
    "web-search-chatbot": {
      "type": "http",
      "url": "https://chat.example.com/api/v1/mcp",
      "headers": { "Authorization": "Bearer wsc_pat_..." }
    }
  }
}
```

The key that holds the servers differs between clients (`mcpServers`,
`servers`); check your client's documentation. A client that can only start
local (stdio) servers, or can only sign in with OAuth, can't connect directly
yet.

## What a client can do

| Tool | Needs | What it does |
|---|---|---|
| `ask` | Ask questions | Answers with web search and the conversation's PDFs, citing sources. Starts a new conversation unless given one. |
| `list_conversations` | Read | Your conversations, most recent first. |
| `get_messages` | Read | One conversation's messages, with each answer's sources. |
| `search_documents` | Read | The passages of a conversation's PDFs most relevant to a query. |

There is also a resource, `conversation://{id}`, which renders a conversation
as Markdown for clients that let you attach resources.

`ask` works exactly like typing in the app:

- The question and answer are saved, and the conversation appears in your
  sidebar.
- Each question uses one message of your daily allowance, and the per-minute
  limit is shared with the browser.
- It needs a confirmed email address.
- A conversation answers one question at a time, so asking into one that is
  already answering fails until it finishes.
- It takes as long as an answer in the app, up to about a minute and a half.
  Clients that show progress show the steps: thinking, searching the web for
  a query, reading documents, writing.
- If the client gives up or you cancel the call, the answer stops too.

Continuing a conversation (`ask` with `conversation_id`) needs both
permissions, because the answer is written with the conversation's history in
view.

## Keeping it safe

- **A token is a password for MCP.** Anyone who has it can do what its
  permissions allow until it expires. Give each client its own token, and
  prefer the shortest lifetime that's practical.
- **Revoke** a token on the AI clients page. Clients using it are refused
  from their next request. Resetting your password revokes every token.
- **Tokens can't sign in.** They work only at `/api/v1/mcp`, never for the
  website or the REST API, so a leaked token can't create more tokens or
  change your account.
- **Quoted text is marked.** Web snippets and PDF passages come back in fields
  named `untrusted_excerpt` and `untrusted_text`, and the server tells the
  client's model that text in those fields is data, not instructions. That
  matters more here than in the app, because your client may have tools that
  can edit files or run commands. Review what it does with them as you would
  anyway.

## Limits

| Limit | Default | Setting |
|---|---|---|
| Requests per token | 120 a minute | `RATE_LIMIT_MCP_REQUESTS`, `RATE_LIMIT_MCP_WINDOW_SECONDS` |
| Failed token checks per client IP | 30 per 5 minutes | `RATE_LIMIT_MCP_AUTH_FAILURES` |
| Token lifetime | 90 days at most | `ACCESS_TOKEN_MAX_TTL_DAYS` |
| Live tokens per account | 10 | `MAX_ACCESS_TOKENS_PER_USER` |
| Questions | Same as the app | `RATE_LIMIT_RUNS`, `DAILY_RUN_LIMIT_PER_USER` |
| Document searches | Share the daily embedding allowance | `DAILY_EMBEDDING_TOKEN_LIMIT_PER_USER` |

`MCP_ENABLED=false` switches the endpoint off (it answers 404). Tokens stay
listed and can still be revoked.

## For operators

- The endpoint is stateless: each request is authenticated and served on
  its own, by any replica. No sticky sessions are needed.
- nginx proxies it with the rest of `/api/`, unbuffered, so progress arrives
  as it happens. `proxy_read_timeout` (120 s) is longer than the run deadline.
- Metrics: `mcp_endpoint_tool_calls_total{tool,outcome}`,
  `mcp_endpoint_tool_duration_seconds{tool}`, and
  `mcp_endpoint_refusals_total{reason}` (`unauthenticated`, `origin`,
  `rate_limited`). Runs started over MCP also count in `runs_total` like any
  other run.
- Logs: one `mcp_tool_call` line per call with the tool, outcome, duration,
  `user`, and `token_id`. Never arguments or results.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401` and the client offers to sign in with OAuth | The token is missing, mistyped, expired, or revoked. Check the AI clients page; create a new one if needed. |
| `403 origin_not_allowed` | A browser-based client is sending an `Origin` other than the app's. Use a desktop or CLI client. |
| `(code: insufficient_scope)` | The token lacks the permission the tool needs. Create a token with it. |
| `(code: email_not_verified)` | Confirm your email address in the app first. |
| `(code: run_in_progress)` | That conversation is already answering (perhaps in your browser). Wait, or ask without `conversation_id`. |
| `(code: daily_quota_exceeded)` | The day's allowance is used up; the message says when a slot frees up. |
| The call times out in the client | Some clients give up on tools sooner than an answer can take. Raise the client's tool timeout. |
