# Frontend

React 19 + TypeScript (strict) + Vite. See the [root README](../README.md) for
setup and the [architecture notes](../docs/architecture.md) for how this fits
together.

```bash
npm ci            # install from the lockfile
npm run dev       # dev server on :5173, proxies /api to the backend
npm run lint      # eslint
npm run typecheck # tsc -b --noEmit
npm test          # vitest
npm run build     # type-check then production build
```

Layout:

```
src/app/       application shell: providers, route table
src/routes/    page components, one per route
src/components/ shared UI (shadcn/ui components land in components/ui)
src/api/       client wrapper and generated client (Phase 3)
src/lib/       small helpers, e.g. cn()
src/styles/    Tailwind entry point and design tokens
src/test/      Vitest setup
tests/e2e/     Playwright specs (Phase 9)
```

Generated files under `src/api/generated/` are never edited by hand — change
the backend schema and run `make generate-client` from the repository root.
