# 3. ESLint + Prettier instead of the Vite template's oxlint

- **Status:** Accepted
- **Date:** 2026-09-06

## Context

`npm create vite@latest --template react-ts` now scaffolds `oxlint`.
`AGENTS.md` §14 names ESLint and Prettier as the frontend quality gates, and
the rules this project needs are type-aware — notably banning `localStorage`
and `sessionStorage` so an auth token can never be persisted (§6).

## Decision

Remove `oxlint` and configure ESLint 10 flat config with
`typescript-eslint` type-checked rules, `eslint-plugin-react-hooks`,
`eslint-plugin-react-refresh`, and `eslint-config-prettier` last. Type-aware
rules are scoped to `**/*.{ts,tsx}`; plain `.js` config files opt out via
`disableTypeChecked`.

## Consequences

- Matches the contract and gives type-aware linting.
- Slower than oxlint on large trees; acceptable at this size.
- Two plugin shape gotchas worth remembering: `eslint-plugin-react-hooks` v7
  exposes flat configs under `.flat[...]` (top-level keys are still eslintrc
  shape), and Vitest's `test` block requires `defineConfig` imported from
  `vitest/config`, not `vite`.
