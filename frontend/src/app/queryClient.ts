import { QueryClient } from '@tanstack/react-query'

/**
 * One client per browser session. Retries are deliberately conservative:
 * a 401 must reach the auth layer rather than being retried into a loop.
 */
export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        retry: 1,
        refetchOnWindowFocus: false,
      },
      mutations: {
        retry: 0,
      },
    },
  })
}
