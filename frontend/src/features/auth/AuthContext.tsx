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
 *
 * The bootstrap runs once but is *subscribed to* on every mount. StrictMode
 * mounts, cleans up, and remounts; if the cleanup discarded the only in-flight
 * result the app would sit on the spinner forever. Holding the promise in a ref
 * lets the remount re-attach to the same work.
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
  // The single bootstrap attempt, started on first mount and reused by any
  // later one. A boolean guard is not enough: it would stop the second mount
  // from *restarting* the work but leave it with no way to see the result.
  const bootstrap = useRef<Promise<User | null> | null>(null)

  useEffect(() => {
    bootstrap.current ??= (async (): Promise<User | null> => {
      const token = await refreshAccessToken()
      if (token === null) return null

      try {
        return await authApi.fetchCurrentUser()
      } catch {
        // A live refresh cookie but an unusable session: drop the token rather
        // than leave the client holding one the API rejects.
        clearSession()
        return null
      }
    })()

    let active = true
    void bootstrap.current.then((restored) => {
      if (!active) return
      setUser(restored)
      setStatus(restored === null ? 'anonymous' : 'authenticated')
    })

    return () => {
      // Only detaches this mount's subscription; the shared promise keeps
      // running and the next mount reads the same result.
      active = false
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

  const refreshUser = useCallback(async () => {
    setUser(await authApi.fetchCurrentUser())
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, signIn, signUp, signOut, refreshUser }),
    [status, user, signIn, signUp, signOut, refreshUser],
  )

  return <AuthContext value={value}>{children}</AuthContext>
}
