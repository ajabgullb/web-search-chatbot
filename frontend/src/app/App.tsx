import { QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'
import { BrowserRouter } from 'react-router'

import { AppRoutes } from '@/app/routes'
import { createQueryClient } from '@/app/queryClient'

export function App() {
  // Created in state so React 19 StrictMode's double-invoke does not build
  // two clients and split the cache.
  const [queryClient] = useState(createQueryClient)

  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  )
}
