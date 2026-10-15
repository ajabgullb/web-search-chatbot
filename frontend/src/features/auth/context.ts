/**
 * The auth context object, kept separate from the provider component.
 *
 * React Fast Refresh only handles a module whose exports are all components,
 * so the context and its types live here and `AuthContext.tsx` exports just
 * `<AuthProvider>`.
 */

import { createContext } from 'react'

import type { User } from '@/api/types'

export type AuthStatus = 'bootstrapping' | 'authenticated' | 'anonymous'

export interface AuthContextValue {
  status: AuthStatus
  user: User | null
  signIn: (email: string, password: string) => Promise<void>
  signUp: (email: string, password: string, displayName: string) => Promise<void>
  signOut: () => Promise<void>
  /** Re-read the account from the server, e.g. after the email is confirmed. */
  refreshUser: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)
