import { QueryClient } from '@tanstack/react-query'

import { ApiError } from '@/api/errors'

/**
 * One client per browser session.
 *
 * Retries are deliberately narrow. A 4xx is the server saying the request
 * itself is wrong — a deleted conversation, a rejected body, an expired
 * session — and repeating it cannot change the answer; it only delays the
 * error the user needs to see. A 401 in particular must reach the auth layer
 * rather than being retried into a loop. Transient failures (5xx, network)
 * still get one more attempt.
 */
export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false
          return failureCount < 1
        },
        refetchOnWindowFocus: false,
      },
      mutations: {
        retry: 0,
      },
    },
  })
}
