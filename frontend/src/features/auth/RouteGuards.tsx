/**
 * Route guards.
 *
 * Both wait out the bootstrap before deciding. Rendering a redirect while the
 * refresh is still in flight would bounce a signed-in user to `/login` on
 * every reload — the session is real, the app just does not know it yet.
 */

import { Navigate, Outlet, useLocation } from 'react-router'

import { FullPageSpinner } from '@/components/FullPageSpinner'
import { useAuth } from '@/features/auth/useAuth'

export function RequireAuth() {
  const { status } = useAuth()
  const location = useLocation()

  if (status === 'bootstrapping') return <FullPageSpinner label="Restoring your session" />

  if (status === 'anonymous') {
    // `state.from` carries the intended destination so the login page can
    // return the user there instead of dumping them on a default page.
    return <Navigate to="/login" replace state={{ from: location }} />
  }

  return <Outlet />
}

export function RedirectIfAuthenticated() {
  const { status } = useAuth()

  if (status === 'bootstrapping') return <FullPageSpinner label="Restoring your session" />
  if (status === 'authenticated') return <Navigate to="/chat" replace />

  return <Outlet />
}
