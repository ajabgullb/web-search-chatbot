/**
 * Session state for the whole application.
 *
 * The bootstrap is the subtle part. On a fresh page load there is no access
 * token — it only ever lived in memory — but the browser may still hold the
 * HttpOnly refresh cookie. So the app makes exactly **one** refresh attempt
 * before deciding whether anyone is signed in.
 *
 * Exactly one, and never a redirect on failure: a failed bootstrap means
 * "nobody is signed in", which is a perfectly normal state for a visitor on
 * the landing page. Treating it as an error is how apps end up bouncing
 * between `/login` and a refresh call forever.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'

import { clearSession, refreshAccessToken, setAccessToken } from '@/api/client'
import type { TokenResponse, User } from '@/api/types'
import * as authApi from '@/features/auth/api'
import { AuthContext } from '@/features/auth/context'
import type { AuthContextValue, AuthStatus } from '@/features/auth/context'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('bootstrapping')
  const [user, setUser] = useState<User | null>(null)
  // React 19 StrictMode mounts effects twice in development; without this the
  // bootstrap would fire two refreshes and the second would look like reuse.
  const bootstrapped = useRef(false)

  useEffect(() => {
    if (bootstrapped.current) return
    bootstrapped.current = true

    let cancelled = false
    void (async () => {
      const token = await refreshAccessToken()
      if (cancelled) return

      if (token === null) {
        setUser(null)
        setStatus('anonymous')
        return
      }

      try {
        setUser(await authApi.fetchCurrentUser())
        setStatus('authenticated')
      } catch {
        clearSession()
        setUser(null)
        setStatus('anonymous')
      }
    })()

    return () => {
      cancelled = true
    }
  }, [])

  const adopt = useCallback((tokens: TokenResponse) => {
    setUser(tokens.user)
    setStatus('authenticated')
  }, [])

  const signIn = useCallback(
    async (email: string, password: string) => {
      adopt(await authApi.login({ email, password }))
    },
    [adopt],
  )

  const signUp = useCallback(
    async (email: string, password: string, displayName: string) => {
      adopt(await authApi.register({ email, password, display_name: displayName }))
    },
    [adopt],
  )

  const signOut = useCallback(async () => {
    try {
      await authApi.logout()
    } catch {
      // The server may already have revoked the session. Either way the local
      // session ends — a failed logout must never strand the user signed in.
    } finally {
      setAccessToken(null)
      setUser(null)
      setStatus('anonymous')
    }
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, signIn, signUp, signOut }),
    [status, user, signIn, signUp, signOut],
  )

  return <AuthContext value={value}>{children}</AuthContext>
}
