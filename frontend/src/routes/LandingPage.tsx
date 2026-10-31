import { ArrowRight, BadgeCheck, Check, FileText, Globe, Sparkles, Zap } from 'lucide-react'
import { Link } from 'react-router'

import { Logo, LogoMark } from '@/components/Logo'
import { ThemeToggle } from '@/components/ThemeToggle'
import { buttonClasses } from '@/components/ui/buttonVariants'
import { useAuth } from '@/features/auth/useAuth'
import { cn } from '@/lib/utils'

const FEATURES = [
  {
    icon: Zap,
    title: 'Searches only when it helps',
    body: 'Greetings and rewrites are answered at once. Fresh, niche, or disputed questions go to the live web first.',
  },
  {
    icon: BadgeCheck,
    title: 'Citations you can open',
    body: 'Every claim drawn from a page carries a numbered chip. Click it to jump to the source, then open the page itself.',
  },
  {
    icon: FileText,
    title: 'Ask your own PDFs',
    body: 'Attach a document to a chat and answers quote it by page, next to the web, kept private to that conversation.',
  },
]

/**
 * What an answer looks like, drawn with static markup. It is an
 * illustration, so assistive technology gets one description of it rather
 * than a tour of fake controls.
 */
function AnswerPreview() {
  return (
    <figure
      aria-label="Example: a question answered with numbered sources"
      className="glass shadow-glow relative mx-auto w-full max-w-xl rounded-3xl border border-border p-5 text-left sm:p-6"
    >
      <div aria-hidden className="flex flex-col gap-4 text-sm">
        <div
          className="animate-rise self-end rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-primary-foreground"
          style={{ animationDelay: '150ms' }}
        >
          Keyset or offset pagination for a large table?
        </div>

        <div className="animate-rise flex gap-3" style={{ animationDelay: '450ms' }}>
          <LogoMark className="mt-0.5 size-7" />
          <div className="min-w-0 flex-1 space-y-3">
            <p className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className="flex size-4 items-center justify-center rounded-full bg-foreground text-background">
                <Check className="size-3" strokeWidth={3} />
              </span>
              Searched “keyset vs offset pagination” · 5 sources
            </p>
            <p className="leading-relaxed">
              Keyset stays fast on deep pages: it seeks straight to an indexed key instead of
              reading and discarding every skipped row
              <span className="citation-marker">
                <span className="citation-button inline-block">1</span>
              </span>
              . Offset is simpler, but slows as the offset grows
              <span className="citation-marker">
                <span className="citation-button inline-block">2</span>
              </span>
              .
            </p>
            <div className="grid gap-2 sm:grid-cols-2">
              {[
                ['U', 'use-the-index-luke.com', 'We need tool support for keyset pagination'],
                ['P', 'postgresql.org', 'LIMIT and OFFSET'],
              ].map(([initial, host, title], index) => (
                <div
                  key={host}
                  className="flex items-start gap-2.5 rounded-xl border border-border bg-card/80 p-2.5"
                >
                  <span
                    className={cn(
                      'flex size-6 shrink-0 items-center justify-center rounded-md text-[11px] font-bold text-white',
                      index === 0 ? 'bg-neutral-700' : 'bg-neutral-500',
                    )}
                  >
                    {initial}
                  </span>
                  <span className="min-w-0">
                    <span className="block truncate text-xs font-medium">{title}</span>
                    <span className="block truncate text-[11px] text-muted-foreground">{host}</span>
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </figure>
  )
}

/** Public landing page. */
export function LandingPage() {
  const { status } = useAuth()
  const signedIn = status === 'authenticated'

  return (
    <div className="relative min-h-dvh overflow-x-clip">
      <header className="glass sticky top-0 z-20 border-b border-border/60">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-5 py-3">
          <Logo />
          <nav aria-label="Account" className="flex items-center gap-1.5">
            <ThemeToggle />
            {signedIn ? (
              <Link to="/chat" className={buttonClasses({ size: 'sm' })}>
                Open the app
              </Link>
            ) : (
              <>
                <Link to="/login" className={buttonClasses({ variant: 'ghost', size: 'sm' })}>
                  Log in
                </Link>
                <Link to="/register" className={buttonClasses({ size: 'sm' })}>
                  Get started
                </Link>
              </>
            )}
          </nav>
        </div>
      </header>

      <main>
        <section className="relative isolate px-5 pt-16 pb-20 sm:pt-24">
          <div aria-hidden className="fade-bottom absolute inset-0 -z-10 overflow-hidden">
            <div className="glow-field animate-drift opacity-70" />
            <div className="dot-grid absolute inset-0" />
          </div>

          <div className="mx-auto max-w-3xl text-center">
            <p className="animate-rise mx-auto inline-flex items-center gap-2 rounded-full border border-border bg-card/70 px-3 py-1 text-xs font-medium text-muted-foreground">
              <Sparkles className="size-3.5 text-primary" aria-hidden />
              Live web search · Cited answers · Your PDFs
            </p>
            <h1
              className="animate-rise mt-6 text-4xl font-semibold tracking-tight text-balance sm:text-6xl"
              style={{ animationDelay: '80ms' }}
            >
              Answers you can check,{' '}
              <span className="text-gradient font-display font-normal italic">
                with the sources attached.
              </span>
            </h1>
            <p
              className="animate-rise mx-auto mt-6 max-w-xl text-lg text-pretty text-muted-foreground"
              style={{ animationDelay: '160ms' }}
            >
              Ask a question and the assistant decides whether it needs the live web. When it
              searches, every claim it draws from a page carries a citation you can open.
            </p>

            <div
              className="animate-rise mt-9 flex flex-wrap justify-center gap-3"
              style={{ animationDelay: '240ms' }}
            >
              {signedIn ? (
                <Link to="/chat" className={buttonClasses({ variant: 'ink', size: 'lg' })}>
                  Continue chatting
                  <ArrowRight className="size-4" aria-hidden />
                </Link>
              ) : (
                <>
                  <Link to="/register" className={buttonClasses({ variant: 'ink', size: 'lg' })}>
                    Create an account
                    <ArrowRight className="size-4" aria-hidden />
                  </Link>
                  <Link to="/login" className={buttonClasses({ variant: 'outline', size: 'lg' })}>
                    I have an account
                  </Link>
                </>
              )}
            </div>
          </div>

          <div className="mt-16 px-1">
            <AnswerPreview />
          </div>
        </section>

        <section aria-labelledby="features" className="mx-auto max-w-6xl px-5 pb-24">
          <h2 id="features" className="sr-only">
            What it does
          </h2>
          <ul className="grid gap-4 md:grid-cols-3">
            {FEATURES.map(({ icon: Icon, title, body }) => (
              <li
                key={title}
                className="group rounded-3xl border border-border bg-card p-6 transition-[transform,box-shadow] duration-200 hover:-translate-y-1 hover:shadow-glow motion-reduce:transition-none motion-reduce:hover:translate-y-0"
              >
                <span className="flex size-11 items-center justify-center rounded-2xl bg-primary/10 text-primary transition-colors group-hover:bg-primary group-hover:text-primary-foreground">
                  <Icon className="size-5" aria-hidden />
                </span>
                <h3 className="mt-5 text-lg font-semibold tracking-tight">{title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{body}</p>
              </li>
            ))}
          </ul>

          <div className="dark relative isolate mt-20 overflow-hidden rounded-3xl bg-background px-6 py-14 text-center text-foreground ring-1 ring-border sm:px-12">
            <div aria-hidden className="glow-field -z-10 opacity-80" />
            <Globe className="mx-auto size-8 text-muted-foreground" aria-hidden />
            <h2 className="mt-4 font-display text-4xl tracking-tight sm:text-5xl">
              Your next question is one tap away.
            </h2>
            <p className="mx-auto mt-3 max-w-md text-muted-foreground">
              Start with a suggestion or ask your own. Sources open in a click.
            </p>
            <Link
              to={signedIn ? '/chat' : '/register'}
              className="bg-ink mt-8 inline-flex h-12 items-center gap-2 rounded-xl px-6 text-[15px] font-medium transition-transform hover:scale-[1.02] focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background focus-visible:outline-none motion-reduce:transition-none"
            >
              {signedIn ? 'Open the app' : 'Start asking'}
              <ArrowRight className="size-4" aria-hidden />
            </Link>
          </div>
        </section>
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-5 py-6 text-sm text-muted-foreground">
          <Logo className="opacity-80" />
          <p>Answers can be wrong. The sources are there so you can check.</p>
        </div>
      </footer>
    </div>
  )
}
