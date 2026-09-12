# 7. Agent core: an explicit graph behind narrow provider boundaries

Date: 2026-09-13

## Status

Accepted.

## Context

`AGENTS.md` §7 specifies a small explicit LangGraph —
`decide_search -> [web_search -> synthesize | answer_direct] -> finalize` — with
hard budgets, citation validation, a PostgreSQL checkpointer, and providers that
never leak vendor types. `PLAN.md` Phase 5 asks for all of it to be proven
through tests *before* any HTTP surface exists. Several choices the contract
leaves open had to be made, and a few small departures from its layout were
worth making.

## Decision

**The graph has no cycle.** Five nodes, every path reaches `finalize` in at most
four steps. The search budget is enforced by a hard counter inside `web_search`,
which runs up to `MAX_SEARCHES_PER_RUN` queries in one visit, rather than by
looping a node. A bounded loop with no back edge cannot run away, and a test can
read the budget off one function instead of reasoning about graph traversal.

**One edge beyond the contract's diagram: `web_search -> answer_direct`.** Taken
when searching yields zero usable sources (outage, empty results, or every URL
rejected). Synthesizing from an empty source list invites exactly the fabricated
citation the pipeline exists to prevent; the direct-answer prompt forbids
citations outright and the validator strips any the model writes anyway.

**Heuristics first, the model for the ambiguous middle.** Greetings and work on
supplied text never search; freshness words, recent years, and explicit requests
for sources always do. Only the rest costs a structured-output model call. If
that call fails the run answers directly — the cheaper wrong answer, since it
spends no search budget.

**No LangChain tool abstraction and no prebuilt agent loop.** §7 permits a tool
abstraction but warns against loops that obscure budgets and failure behaviour.
`app/agent/tools/search.py` is a plain module the node calls directly. The model
never emits tool calls, so it can never choose a tool argument outside the
validated decision schema.

**Tavily over its REST API with `httpx`, not the `tavily-python` SDK.** One
shared pooled client built in the lifespan, our own timeout, and error
classification by HTTP status. Parsing is deliberately forgiving: an unexpected
field costs a missing publisher, not a failed run. CI never calls the live API,
so the mapping is covered by `httpx.MockTransport` tests rather than verified
against the service.

**Failures are classified at the provider boundary.** Both providers raise only
`*TransientError` (timeouts, connection failures, 408/409/425/429/5xx — worth one
retry) or `*PermanentError` (everything else). An unrecognised exception is
permanent: retrying an error we do not understand is how a bad request becomes a
bill. The OpenAI SDK's own retries are disabled so the retry budget has one owner.

**State holds `TypedDict`s of JSON-native values, not dataclasses.** LangGraph's
checkpoint serializer round-trips custom classes with a deprecation warning and
states they will be blocked; a checkpoint that cannot be read back is a run that
cannot be recovered. Identity, clients, and limits travel in runtime context and
are never checkpointed.

**The checkpointer has its own psycopg pool.** `langgraph-checkpoint-postgres`
speaks psycopg while the application engine speaks asyncpg. The DSN is
translated rather than forcing one driver on both, and the separate pool means a
stalled checkpoint write cannot starve the API's request connections.
`setup()` runs at startup, which suits the single dev replica; production moves
it into the release job alongside Alembic (Phase 8).

**Layout departures from §4**, each small: `app/agent/citations.py` (validation
is its own testable concern) and `app/agent/checkpointer.py` are added;
`app/agent/events.py` is deferred to Phase 6, which is the first phase that
emits events.

## Consequences

- Phase 6 must add a streaming method to `LLMProvider`; Phase 5 exposes only
  `complete` and `structured`, because nothing yet consumes a token stream.
- API startup now opens a checkpointer connection and, on first boot, creates
  LangGraph's tables. The API already required a database to be ready.
- The degraded rule does not detect "factual claims" in prose — no reliable test
  exists — so a searched answer of 160+ characters with no valid citation is
  flagged. A needless caveat is the accepted cost of never showing an answer
  that merely looks sourced.
- Freshness heuristics will sometimes search when it was not needed ("still",
  "current" used non-temporally). That costs one search, not correctness.
- Tavily does not return a publisher, so `publisher` is currently always null.
