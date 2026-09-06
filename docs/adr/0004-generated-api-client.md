# 4. Generate types with openapi-typescript, hand-write the client

- **Status:** Accepted
- **Date:** 2026-09-06

## Context

`AGENTS.md` §8 requires the frontend client to be generated from the OpenAPI
schema and regenerated whenever the public schema changes, and §16 forbids
hand-editing generated files. It does not name a tool.

Full client generators (`@hey-api/openapi-ts`, `orval`) emit both types *and*
a runtime. But this app's runtime needs are unusual and security-critical: an
access token held only in memory, a single-flight refresh, and a strict
one-retry rule. Bending a generated runtime into that shape would put the most
security-sensitive code in a file nobody is allowed to edit.

`openapi-typescript@7.13.0` peer-depends on TypeScript `^5.x`; the project is
on TypeScript 6, so plain `npm install` fails to resolve.

## Decision

Generate **types only** — `src/api/generated/schema.d.ts` — with
`openapi-typescript`, and hand-write `src/api/client.ts`.

Resolve the peer conflict with an npm `overrides` entry pointing the tool's
`typescript` peer at the project's own copy, rather than `--legacy-peer-deps`
(which would make `npm ci` unreliable) or fetching the tool at build time
(which would put a codegen step outside the lockfile). Generation was verified
to work against TypeScript 6.0.3, and `npm ci` was verified from a clean
`node_modules`.

CI regenerates and runs `git diff --exit-code src/api/generated`, so a backend
schema change that never reached the client fails the build.

## Consequences

- The generated artefact is purely declarative, so "never hand-edit generated
  files" is trivially satisfiable — there is no logic in it to be tempted by.
- Auth transport logic is ordinary reviewable, testable code.
- The override must be revisited when `openapi-typescript` supports TS 6
  officially, or if a future version breaks against it. The pinned version and
  the CI drift check make that failure loud rather than silent.
