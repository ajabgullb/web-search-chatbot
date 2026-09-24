# 12. Testing and evaluation: real code paths, scripted paid dependencies

Date: 2026-09-24

## Status

Accepted.

## Context

AGENTS.md §13 requires end-to-end tests with fake providers, contract tests
for the schema and the generated client, and an evaluation dataset with
deterministic CI gates and separate model-based runs. It also forbids any test
that needs a paid API.

The risk with fakes is testing the fakes. The risk with evals is numbers that
look rigorous and measure nothing.

## Decision

**Replace exactly two things: the LLM and search providers.** Everything
else runs as in production in every layer: the compiled graph, auth, SSE,
persistence, cancellation, and the browser.

- The **end-to-end server** (`backend/tests/e2e_server.py`) is the real app
  with the two provider builders swapped. It lives under `tests/`, so the
  stand-ins never reach an image. It uses its own `_e2e` database, migrated
  by Alembic and emptied at start, and its own ports. The scripted model
  answers by what it is asked (a question needing search gets a cited answer,
  a "slow" one streams long enough to be stopped), so each spec picks its
  path through the real graph.
- **Evals have two subsets over one dataset.**
  - `ci` scripts the providers per case. It can only judge what does not
    depend on the model's judgement, so that is all it scores: heuristic
    routing (and that the heuristics made the decision), citation validity
    against retrieved sources, degraded notices, injection-proof queries and
    fencing, context reaching the prompt, and prompt leaks. It gates at
    100%.
  - `live` uses the configured model and an LLM judge for the rubric score
    and unsupported-claim rate. It has looser gates and is never run in CI.
- **Citation precision** needs what the model *emitted*, not the cleaned
  answer, so the harness wraps the provider and records raw output.
- **Contract tests on both sides.** The backend pins the committed schema and
  its rules. The frontend pins its event-type list to the schema's union,
  which the type system alone cannot do. Vitest's `provide` hands the schema
  to the test, so the dev server's file-access guard is not loosened.

## Consequences

The new layers found five defects that existing tests had missed:

1. **Unmarked unsourced answers.** A search that found no sources produced an
   answer that was neither marked degraded nor noticed. The eval dataset's
   empty-results case caught it (fixed in `web_search`).
2. **Lost sign-in destination.** Signing in lost the page the user was going
   to. The existing unit test used `/chat`, the default, as its
   "destination", so it could not fail. Playwright caught it.
3. **Wrong error content type.** The run endpoint documented its error
   envelope as `text/event-stream`. The contract test caught it.
4. **Plaintext SMTP.** Password-reset email went unencrypted. This was found
   while writing the production stack rather than by these layers, and is
   recorded in [ADR-0011](0011-production-failure-modes.md).
5. **High-severity npm advisory.** A dev-only transitive dependency had one.
   The dependency audit found it.

What this still cannot show: whether real models answer well. The `ci` evals
say nothing about answer quality by design. Run `make eval-live` after any
prompt or model change and compare reports of the same dataset version.
