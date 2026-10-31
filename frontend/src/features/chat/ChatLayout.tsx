/**
 * Shell shared by /chat and /chat/:conversationId.
 *
 * The sidebar lives here rather than in each page so switching conversations
 * does not unmount and refetch the history list.
 */

import { Menu, PanelLeftOpen } from 'lucide-react'
import { useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { Outlet } from 'react-router'

import { Logo } from '@/components/Logo'
import { Button } from '@/components/ui/Button'
import { VerifyEmailBanner } from '@/features/auth/VerifyEmailBanner'
import { RunStoreProvider } from '@/features/chat/RunStoreProvider'
import { Sidebar } from '@/features/chat/Sidebar'
import { cn } from '@/lib/utils'

const COLLAPSED_KEY = 'sidebar-collapsed'

function readCollapsed(): boolean {
  try {
    // eslint-disable-next-line no-restricted-properties -- layout preference, not a credential
    return window.localStorage.getItem(COLLAPSED_KEY) === 'true'
  } catch {
    // Private browsing or blocked site data: start expanded.
    return false
  }
}

function writeCollapsed(collapsed: boolean): void {
  try {
    // eslint-disable-next-line no-restricted-properties -- layout preference, not a credential
    window.localStorage.setItem(COLLAPSED_KEY, String(collapsed))
  } catch {
    // Not remembered across reloads; the toggle still works for this visit.
  }
}

export function ChatLayout() {
  // Two independent states: `sidebarOpen` is the off-canvas panel on small
  // screens, `collapsed` hides the permanent column on `md` and up.
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const expandButton = useRef<HTMLButtonElement>(null)
  const collapseButton = useRef<HTMLButtonElement>(null)

  function setSidebarCollapsed(next: boolean) {
    // The button that was clicked is about to disappear, which would drop
    // keyboard focus to <body>. Commit the change synchronously so its
    // counterpart exists, then hand focus to it.
    flushSync(() => setCollapsed(next))
    writeCollapsed(next)
    ;(next ? expandButton : collapseButton).current?.focus()
  }

  return (
    <RunStoreProvider>
      <div className="flex h-dvh overflow-hidden">
        {/* The panel closes itself from the handlers that navigate, rather than
          from an effect watching the location: the close is caused by the
          click, and syncing it through render-then-effect is both a cascading
          render and a frame of the wrong UI. */}
        <Sidebar
          open={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
          collapsed={collapsed}
          onCollapse={() => setSidebarCollapsed(true)}
          collapseButtonRef={collapseButton}
        />

        <div className="flex min-w-0 flex-1 flex-col">
          <header
            className={cn(
              'glass flex items-center gap-2 border-b border-border px-3 py-2',
              !collapsed && 'md:hidden',
            )}
          >
            <Button
              size="icon"
              variant="ghost"
              className="md:hidden"
              aria-label="Open conversation list"
              aria-expanded={sidebarOpen}
              onClick={() => setSidebarOpen(true)}
            >
              <Menu className="size-4" aria-hidden />
            </Button>
            <Button
              ref={expandButton}
              size="icon"
              variant="ghost"
              className="hidden md:inline-flex"
              aria-label="Expand sidebar"
              onClick={() => setSidebarCollapsed(false)}
            >
              <PanelLeftOpen className="size-4" aria-hidden />
            </Button>
            <Logo />
          </header>

          <VerifyEmailBanner />
          <Outlet />
        </div>
      </div>
    </RunStoreProvider>
  )
}
