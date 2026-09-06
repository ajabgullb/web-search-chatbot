import { useContext } from 'react'

import { AuthContext } from '@/features/auth/context'
import type { AuthContextValue } from '@/features/auth/context'

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (value === null) {
    throw new Error('useAuth must be used inside an <AuthProvider>')
  }
  return value
}
