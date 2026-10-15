import { MessageSquarePlus } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router'

import { Button } from '@/components/ui/Button'
import { useAuth } from '@/features/auth/useAuth'
import { greetingText, pickGreetingIndex } from '@/features/chat/greetings'
import { useCreateConversation } from '@/features/chat/queries'

// The greeting shown last time this page mounted, so the next visit picks a
// different one. Module state rather than storage: it only needs to outlive a
// route change, and a reload is free to start over.
let lastGreeting = -1

/**
 * `/chat` with no conversation selected.
 *
 * Deliberately does *not* auto-create a conversation on mount: a user who
 * lands here, reloads, or signs out and back in would leave a trail of empty
 * threads. The thread is created when they ask for one.
 */
export function ChatPage() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const createConversation = useCreateConversation()

  // Chosen once per visit. The initializer only reads `lastGreeting`; the
  // effect records the choice, so StrictMode's double call cannot skip one.
  const [greeting] = useState(() => pickGreetingIndex(lastGreeting))
  useEffect(() => {
    lastGreeting = greeting
  }, [greeting])

  return (
    <main className="flex flex-1 items-center justify-center p-6">
      <div className="max-w-md text-center">
        <h1 className="text-2xl font-semibold">
          {greetingText(greeting, user?.display_name, new Date())}
        </h1>
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
