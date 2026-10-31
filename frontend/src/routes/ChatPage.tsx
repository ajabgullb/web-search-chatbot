import { FileText, Globe, Lightbulb, Map as MapIcon, Scale } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router'

import { LogoMark } from '@/components/Logo'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { useAuth } from '@/features/auth/useAuth'
import { Composer } from '@/features/chat/Composer'
import { greetingText, pickGreetingIndex } from '@/features/chat/greetings'
import { useCreateConversation } from '@/features/chat/queries'
import { useRunStore } from '@/features/chat/useLiveRun'

// The greeting shown last time this page mounted, so the next visit picks a
// different one. Module state rather than storage: it only needs to outlive a
// route change, and a reload is free to start over.
let lastGreeting = -1

/**
 * One-tap starters. Each one shows off a different thing the assistant does
 * well — fresh news, sourced comparison, plain explanation, planning — so a
 * first visit ends with an answer instead of a blank box.
 */
const SUGGESTIONS = [
  {
    icon: Globe,
    title: 'Catch up on the news',
    prompt: 'What are the biggest science news stories this week?',
  },
  {
    icon: Scale,
    title: 'Compare with sources',
    prompt: 'Compare Rust and Go for a web backend, with sources.',
  },
  {
    icon: Lightbulb,
    title: 'Explain it simply',
    prompt: 'Explain how mRNA vaccines work, as if I were new to biology.',
  },
  {
    icon: MapIcon,
    title: 'Plan something',
    prompt: "Plan three days in Lisbon, including what's on this month.",
  },
]

/**
 * `/chat` with no conversation selected.
 *
 * Deliberately does *not* auto-create a conversation on mount: a user who
 * lands here, reloads, or signs out and back in would leave a trail of empty
 * threads. A thread is created by the first question asked here — typed or
 * picked from a suggestion — or by "New chat" for one that starts with a PDF.
 */
export function ChatPage() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const store = useRunStore()
  const createConversation = useCreateConversation()

  // Chosen once per visit. The initializer only reads `lastGreeting`; the
  // effect records the choice, so StrictMode's double call cannot skip one.
  const [greeting] = useState(() => pickGreetingIndex(lastGreeting))
  useEffect(() => {
    lastGreeting = greeting
  }, [greeting])

  /** Create the thread, start the answer, then open it: one action. */
  function ask(content: string) {
    createConversation.mutate(undefined, {
      onSuccess: (conversation) => {
        // Sent before navigating, so the conversation page mounts with the
        // live turn already under way rather than flashing an empty thread.
        store.send(conversation.id, content)
        void navigate(`/chat/${conversation.id}`)
      },
    })
  }

  const busy = createConversation.isPending

  return (
    <main className="relative isolate flex flex-1 flex-col overflow-y-auto">
      <div aria-hidden className="fade-bottom absolute inset-x-0 top-0 -z-10 h-96 overflow-hidden">
        <div className="glow-field animate-drift opacity-40" />
      </div>

      <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center px-4 py-10">
        <div className="animate-rise text-center">
          <LogoMark className="shadow-glow mx-auto size-12 rounded-[14px]" />
          <h1 className="mt-6 font-display text-4xl tracking-tight text-balance sm:text-5xl">
            <span className="text-gradient">
              {greetingText(greeting, user?.display_name, new Date())}
            </span>
          </h1>
          <p className="mt-3 text-[15px] text-muted-foreground">
            Ask a question and the assistant will search the web when it helps, with sources you can
            check.
          </p>
        </div>

        <div className="animate-rise mt-8" style={{ animationDelay: '80ms' }}>
          <Composer variant="hero" onSubmit={ask} busy={busy} placeholder="Ask anything…" />
        </div>

        {createConversation.isError ? (
          <Alert className="mt-4">The conversation could not be started. Try again.</Alert>
        ) : null}

        <ul
          aria-label="Suggestions"
          className="animate-rise mt-6 grid gap-3 sm:grid-cols-2"
          style={{ animationDelay: '160ms' }}
        >
          {SUGGESTIONS.map(({ icon: Icon, title, prompt }) => (
            <li key={title}>
              <button
                type="button"
                disabled={busy}
                onClick={() => ask(prompt)}
                className="group flex h-full w-full items-start gap-3 rounded-2xl border border-border bg-card/70 p-4 text-left transition-[transform,border-color,box-shadow] duration-200 hover:-translate-y-0.5 hover:border-ring/40 hover:shadow-glow focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-60 motion-reduce:transition-none motion-reduce:hover:translate-y-0"
              >
                <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary transition-colors group-hover:bg-primary group-hover:text-primary-foreground">
                  <Icon className="size-4" aria-hidden />
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-medium">{title}</span>
                  <span className="mt-0.5 block text-sm text-muted-foreground">{prompt}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>

        <div className="mt-8 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 text-sm text-muted-foreground">
          <FileText className="size-4" aria-hidden />
          <span>Want to start from a PDF?</span>
          <Button
            variant="ghost"
            size="sm"
            className="text-primary"
            disabled={busy}
            onClick={() =>
              createConversation.mutate(undefined, {
                onSuccess: (conversation) => void navigate(`/chat/${conversation.id}`),
              })
            }
          >
            New chat
          </Button>
        </div>
      </div>
    </main>
  )
}
