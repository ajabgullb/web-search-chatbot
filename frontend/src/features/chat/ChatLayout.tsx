/**
 * Shell shared by /chat and /chat/:conversationId.
 *
 * The sidebar lives here rather than in each page so switching conversations
 * does not unmount and refetch the history list.
 */

import { Menu } from 'lucide-react'
import { useState } from 'react'
import { Outlet } from 'react-router'

import { Button } from '@/components/ui/Button'
import { RunStoreProvider } from '@/features/chat/RunStoreProvider'
import { Sidebar } from '@/features/chat/Sidebar'

export function ChatLayout() {
  const [sidebarOpen, setSidebarOpen] = useState(false)

  return (
    <RunStoreProvider>
      <div className="flex h-dvh overflow-hidden">
        {/* The panel closes itself from the handlers that navigate, rather than
          from an effect watching the location: the close is caused by the
          click, and syncing it through render-then-effect is both a cascading
          render and a frame of the wrong UI. */}
        <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex items-center gap-2 border-b border-border px-3 py-2 md:hidden">
            <Button
              size="icon"
              variant="ghost"
              aria-label="Open conversation list"
              aria-expanded={sidebarOpen}
              onClick={() => setSidebarOpen(true)}
            >
              <Menu className="size-4" aria-hidden />
            </Button>
            <span className="text-sm font-semibold">Web Search Chatbot</span>
          </header>

          <Outlet />
        </div>
      </div>
    </RunStoreProvider>
  )
}
