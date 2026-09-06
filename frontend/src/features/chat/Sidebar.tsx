/**
 * Conversation history, new chat, and the account block.
 *
 * On small screens it is an off-canvas panel; on `md` and up it is a permanent
 * column. One component either way — a second mobile-only tree would be two
 * things to keep in sync.
 */

import { Plus, X } from 'lucide-react'
import { useNavigate, useParams } from 'react-router'

import { Button } from '@/components/ui/Button'
import { ThemeToggle } from '@/components/ThemeToggle'
import { ConversationItem } from '@/features/chat/ConversationItem'
import {
  useConversations,
  useCreateConversation,
  useDeleteConversation,
  useRenameConversation,
} from '@/features/chat/queries'
import { useAuth } from '@/features/auth/useAuth'
import { cn } from '@/lib/utils'

export function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const { user, signOut } = useAuth()

  const conversations = useConversations()
  const createConversation = useCreateConversation()
  const renameConversation = useRenameConversation()
  const deleteConversation = useDeleteConversation()

  const items = conversations.data?.items ?? []

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
        <div className="fixed inset-0 z-30 bg-black/40 md:hidden" onClick={onClose} aria-hidden />
      ) : null}

      <nav
        aria-label="Conversations"
        className={cn(
          'fixed inset-y-0 left-0 z-40 flex w-72 flex-col border-r border-border bg-card',
          'transition-transform duration-200 motion-reduce:transition-none',
          'md:static md:z-auto md:translate-x-0',
          open ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        <div className="flex items-center gap-2 p-3">
          <Button
            className="flex-1"
            size="sm"
            onClick={startNewChat}
            disabled={createConversation.isPending}
          >
            <Plus className="size-4" aria-hidden />
            New chat
          </Button>
          <Button
            size="icon"
            variant="ghost"
            className="md:hidden"
            onClick={onClose}
            aria-label="Close conversation list"
          >
            <X className="size-4" aria-hidden />
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto px-1">
          {conversations.isPending ? (
            <p className="px-3 py-2 text-sm text-muted-foreground">Loading conversations…</p>
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

          {!conversations.isPending && items.length === 0 ? (
            <p className="px-3 py-2 text-sm text-muted-foreground">No conversations yet.</p>
          ) : null}

          <ul className="space-y-0.5">
            {items.map((conversation) => (
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
          <p className="truncate text-sm font-medium">{user?.display_name}</p>
          <p className="truncate text-xs text-muted-foreground">{user?.email}</p>
          <div className="mt-2 flex items-center gap-2">
            <Button variant="outline" size="sm" className="flex-1" onClick={() => void signOut()}>
              Sign out
            </Button>
            <ThemeToggle />
          </div>
        </div>
      </nav>
    </>
  )
}
