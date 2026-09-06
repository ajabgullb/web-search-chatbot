import { MessageSquarePlus } from 'lucide-react'
import { useNavigate } from 'react-router'

import { Button } from '@/components/ui/Button'
import { useCreateConversation } from '@/features/chat/queries'

/**
 * `/chat` with no conversation selected.
 *
 * Deliberately does *not* auto-create a conversation on mount: a user who
 * lands here, reloads, or signs out and back in would leave a trail of empty
 * threads. The thread is created when they ask for one.
 */
export function ChatPage() {
  const navigate = useNavigate()
  const createConversation = useCreateConversation()

  return (
    <main className="flex flex-1 items-center justify-center p-6">
      <div className="max-w-md text-center">
        <h1 className="text-2xl font-semibold">Start a conversation</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Ask a question and the assistant will search the web when it helps, with sources you can
          check.
        </p>
        <Button
          className="mt-6"
          disabled={createConversation.isPending}
          onClick={() =>
            createConversation.mutate(undefined, {
              onSuccess: (conversation) => void navigate(`/chat/${conversation.id}`),
            })
          }
        >
          <MessageSquarePlus className="size-4" aria-hidden />
          New chat
        </Button>
      </div>
    </main>
  )
}
