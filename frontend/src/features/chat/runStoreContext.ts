import { createContext } from 'react'

import type { RunStore } from '@/features/chat/runStore'

/** Provided by `RunStoreProvider` around the chat routes. */
export const RunStoreContext = createContext<RunStore | null>(null)
