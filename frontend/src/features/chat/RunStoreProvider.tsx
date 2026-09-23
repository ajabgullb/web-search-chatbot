import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'

import { RunStore } from '@/features/chat/runStore'
import { RunStoreContext } from '@/features/chat/runStoreContext'

/**
 * One run store for the chat area.
 *
 * Mounted around the chat layout, so answers keep streaming while the user
 * moves between conversations, and every stream is hung up when they leave
 * the chat altogether (signing out unmounts it). Hanging up is what tells the
 * server to stop spending on answers nobody will read.
 */
export function RunStoreProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  // In state, not a ref or module: StrictMode's double render must not build
  // two stores and split the streams between them.
  const [store] = useState(() => new RunStore(queryClient))

  useEffect(() => () => store.abortAll(), [store])

  return <RunStoreContext.Provider value={store}>{children}</RunStoreContext.Provider>
}
