import { Link } from 'react-router'

/**
 * Public landing page. Phase 3 wires the calls to action to the real
 * `/register` and `/login` routes.
 */
export function LandingPage() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-3xl flex-col justify-center gap-8 px-6 py-16">
      <header className="space-y-4">
        <p className="text-sm font-medium tracking-wide text-muted-foreground uppercase">
          Web Search Chatbot
        </p>
        <h1 className="text-4xl font-semibold text-balance sm:text-5xl">
          Answers you can check, with the sources attached.
        </h1>
        <p className="max-w-prose text-lg text-muted-foreground">
          Ask a question and the assistant decides whether it needs the live web. When it searches,
          every claim it draws from a page carries a citation you can open.
        </p>
      </header>

      <nav aria-label="Get started" className="flex flex-wrap gap-3">
        <Link
          to="/register"
          className="rounded-md bg-primary px-5 py-2.5 text-sm font-medium text-primary-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none"
        >
          Create an account
        </Link>
        <Link
          to="/login"
          className="rounded-md border border-border px-5 py-2.5 text-sm font-medium focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none"
        >
          Log in
        </Link>
      </nav>
    </main>
  )
}
