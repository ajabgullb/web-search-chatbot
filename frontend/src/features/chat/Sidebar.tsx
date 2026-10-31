/**
 * Conversation history, new chat, and the account block.
 *
 * On small screens it is an off-canvas panel; on `md` and up it is a column
 * the user can collapse. One component either way — a second mobile-only tree would be two
 * things to keep in sync.
 */

import { LogOut, MessageSquareDashed, PanelLeftClose, SquarePen, X } from 'lucide-react'
import type { Ref } from 'react'
import { useNavigate, useParams } from 'react-router'

import { Logo } from '@/components/Logo'
import { Button } from '@/components/ui/Button'
import { ThemeToggle } from '@/components/ThemeToggle'
import { ConversationItem } from '@/features/chat/ConversationItem'
import { groupByRecency } from '@/features/chat/recency'
import {
  useConversations,
  useCreateConversation,
  useDeleteConversation,
  useRenameConversation,
} from '@/features/chat/queries'
import { useAuth } from '@/features/auth/useAuth'
import { cn } from '@/lib/utils'

export function Sidebar({
  open,
  onClose,
  collapsed,
  onCollapse,
  collapseButtonRef,
}: {
  open: boolean
  onClose: () => void
  collapsed: boolean
  onCollapse: () => void
  collapseButtonRef: Ref<HTMLButtonElement>
}) {
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const { user, signOut } = useAuth()

  const conversations = useConversations()
  const createConversation = useCreateConversation()
  const renameConversation = useRenameConversation()
  const deleteConversation = useDeleteConversation()

  const items = conversations.data?.items ?? []
  const groups = groupByRecency(items, new Date())
  const initial = (user?.display_name.trim()[0] ?? '?').toUpperCase()

  function startNewChat() {
    createConversation.mutate(undefined, {
      onSuccess: (conversation) => {
        onClose()
        void navigate(`/chat/${conversation.id}`)
      },
    })
  }

  function handleDelete(id: string) {
    deleteConversation.mutate(id, {
      onSuccess: () => {
        // Leaving the user staring at a deleted thread would 404 on refresh.
        if (id === conversationId) void navigate('/chat', { replace: true })
      },
    })
  }

  return (
    <>
      {/* Scrim, mobile only. aria-hidden because the close button in the
          panel is the accessible way out. */}
      {open ? (
        <div
          className="animate-fade fixed inset-0 z-30 bg-black/50 backdrop-blur-sm md:hidden"
          onClick={onClose}
          aria-hidden
        />
      ) : null}

      <nav
        aria-label="Conversations"
        className={cn(
          'fixed inset-y-0 left-0 z-40 flex w-72 flex-col border-r border-border bg-card md:bg-muted/40',
          'transition-transform duration-300 ease-out motion-reduce:transition-none',
          'md:static md:z-auto md:translate-x-0',
          open ? 'translate-x-0' : '-translate-x-full',
          // Collapsing removes the column from layout and from the
          // accessibility tree. It never affects the mobile panel.
          collapsed && 'md:hidden',
        )}
      >
        <div className="flex items-center justify-between gap-2 px-3 pt-3 pb-2">
          <Logo className="pl-1" />
          <Button
            size="icon"
            variant="ghost"
            className="md:hidden"
            onClick={onClose}
            aria-label="Close conversation list"
          >
            <X className="size-4" aria-hidden />
          </Button>
          <Button
            ref={collapseButtonRef}
            size="icon"
            variant="ghost"
            className="hidden text-muted-foreground md:inline-flex"
            onClick={onCollapse}
            aria-label="Collapse sidebar"
          >
            <PanelLeftClose className="size-4" aria-hidden />
          </Button>
        </div>

        <div className="px-3 pb-3">
          <Button
            variant="ink"
            className="w-full justify-start"
            onClick={startNewChat}
            disabled={createConversation.isPending}
          >
            <SquarePen className="size-4" aria-hidden />
            New chat
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 pb-2">
          {conversations.isPending ? (
            // Skeleton rows hint at the list's shape; the sentence is what a
            // screen reader hears.
            <div className="space-y-2 px-2 py-2">
              <p className="sr-only">Loading conversations…</p>
              {[72, 56, 64, 48].map((width) => (
                <div
                  key={width}
                  aria-hidden
                  className="h-8 animate-pulse rounded-lg bg-muted motion-reduce:animate-none"
                  style={{ width: `${width}%` }}
                />
              ))}
            </div>
          ) : null}

          {conversations.isError ? (
            <div className="px-3 py-2">
              <p className="text-sm text-destructive">Could not load conversations.</p>
              <Button
                size="sm"
                variant="outline"
                className="mt-2"
                onClick={() => void conversations.refetch()}
              >
                Try again
              </Button>
            </div>
          ) : null}

          {!conversations.isPending && !conversations.isError && items.length === 0 ? (
            <div className="mx-2 mt-2 flex flex-col items-center gap-2 rounded-2xl border border-dashed border-border px-4 py-8 text-center">
              <MessageSquareDashed className="size-6 text-muted-foreground" aria-hidden />
              <p className="text-sm text-muted-foreground">No conversations yet.</p>
            </div>
          ) : null}

          {groups.map((group) => (
            <section key={group.label} aria-label={group.label} className="mt-3 first:mt-1">
              <h2 className="px-2 pb-1 text-[11px] font-semibold tracking-wider text-muted-foreground uppercase">
                {group.label}
              </h2>
              <ul className="space-y-0.5">
                {group.items.map((conversation) => (
                  <ConversationItem
                    key={conversation.id}
                    conversation={conversation}
                    isActive={conversation.id === conversationId}
                    onRename={(title) => renameConversation.mutate({ id: conversation.id, title })}
                    onDelete={() => handleDelete(conversation.id)}
                    onNavigate={onClose}
                  />
                ))}
              </ul>
            </section>
          ))}

          {conversations.hasNextPage ? (
            <div className="p-2">
              <Button
                variant="outline"
                size="sm"
                className="w-full"
                disabled={conversations.isFetchingNextPage}
                onClick={() => void conversations.fetchNextPage()}
              >
                {conversations.isFetchingNextPage ? 'Loading…' : 'Load more'}
              </Button>
            </div>
          ) : null}
        </div>

        {/* Account block. A plain always-visible footer rather than a popup
            menu: same actions, no focus-trap or dismissal behaviour to get
            wrong, and it reads fine on mobile. */}
        <div className="border-t border-border p-3">
          <div className="flex items-center gap-3 rounded-xl p-1.5">
            <span
              aria-hidden
              className="bg-ink flex size-9 shrink-0 items-center justify-center rounded-full text-sm font-semibold"
            >
              {initial}
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{user?.display_name}</p>
              <p className="truncate text-xs text-muted-foreground">{user?.email}</p>
            </div>
            <ThemeToggle />
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="mt-1 w-full justify-start text-muted-foreground"
            onClick={() => void signOut()}
          >
            <LogOut className="size-4" aria-hidden />
            Sign out
          </Button>
        </div>
      </nav>
    </>
  )
}
