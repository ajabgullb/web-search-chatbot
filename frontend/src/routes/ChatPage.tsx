import { useAuth } from '@/features/auth/useAuth'
import { Button } from '@/components/ui/Button'
import { ThemeToggle } from '@/components/ThemeToggle'

/**
 * Placeholder for the authenticated area.
 *
 * It exists so the route guard has something real to protect. Phase 4 replaces
 * it with the sidebar and conversation view.
 */
export function ChatPage() {
  const { user, signOut } = useAuth()

  return (
    <div className="min-h-dvh">
      <header className="flex items-center justify-between border-b border-border px-6 py-4">
        <h1 className="text-sm font-semibold">Web Search Chatbot</h1>
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">{user?.display_name}</span>
          <ThemeToggle />
          <Button variant="outline" size="sm" onClick={() => void signOut()}>
            Sign out
          </Button>
        </div>
      </header>

      <main className="mx-auto max-w-2xl px-6 py-16 text-center">
        <h2 className="text-2xl font-semibold">You&rsquo;re signed in</h2>
        <p className="mt-2 text-muted-foreground">
          Signed in as {user?.email}. Conversations and the chat interface arrive in Phase 4.
        </p>
      </main>
    </div>
  )
}
