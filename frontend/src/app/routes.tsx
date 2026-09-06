import { Route, Routes } from 'react-router'

import { LandingPage } from '@/routes/LandingPage'
import { NotFoundPage } from '@/routes/NotFoundPage'

/**
 * Route table. Phase 3 adds the auth routes and Phase 4 the guarded
 * `/chat` routes; the catch-all always stays last.
 */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<LandingPage />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}
