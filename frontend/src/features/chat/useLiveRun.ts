import { useContext, useSyncExternalStore } from 'react'

import type { LiveRun, RunStore } from '@/features/chat/runStore'
import { RunStoreContext } from '@/features/chat/runStoreContext'

export function useRunStore(): RunStore {
  const store = useContext(RunStoreContext)
  if (!store) throw new Error('useRunStore must be used inside RunStoreProvider')
  return store
}

/** The live run for one conversation, re-rendering only when it changes. */
export function useLiveRun(conversationId: string | undefined): LiveRun | null {
  const store = useRunStore()
  return useSyncExternalStore(store.subscribe, () =>
    conversationId === undefined ? null : store.get(conversationId),
  )
}
