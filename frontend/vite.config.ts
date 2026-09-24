import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
// vitest/config re-exports Vite's defineConfig with the `test` block typed.
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // 0.0.0.0 so the dev server is reachable from outside the container.
    host: true,
    port: 5173,
    strictPort: true,
    // Same-origin API calls in development: no CORS preflight, and the
    // refresh cookie behaves exactly as it will behind a production proxy.
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: false,
      },
    },
  },
  test: {
    // The committed API schema, handed to contract tests. Read here, in Node,
    // because the dev server rightly refuses files outside the project. Only
    // under Vitest: in the Docker images only frontend/ exists, and the dev
    // server and `vite build` load this same config.
    provide: {
      openapiSchema: process.env.VITEST
        ? readFileSync(new URL('../openapi.json', import.meta.url), 'utf-8')
        : '',
    },
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: true,
    // Playwright owns tests/e2e; Vitest must not try to run them.
    exclude: ['node_modules', 'dist', 'tests/e2e'],
  },
})
