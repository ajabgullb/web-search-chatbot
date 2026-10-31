import { BadgeCheck, FileText, Globe } from 'lucide-react'
import { Link } from 'react-router'
import type { ReactNode } from 'react'

import { Logo } from '@/components/Logo'
import { ThemeToggle } from '@/components/ThemeToggle'

interface AuthLayoutProps {
  title: string
  description?: ReactNode
  children: ReactNode
  footer?: ReactNode
}

const HIGHLIGHTS = [
  { icon: Globe, text: 'Searches the live web when your question needs it' },
  { icon: BadgeCheck, text: 'Every sourced claim links to the page it came from' },
  { icon: FileText, text: 'Drop in a PDF and ask about it, page by page' },
]

/**
 * Shared frame for the unauthenticated auth pages: the form on the left, a
 * brand panel on the right from `lg` up. The panel is decoration and a
 * reminder of what the account is for; everything needed to sign in is in
 * the left column, so small screens lose nothing by not showing it.
 */
export function AuthLayout({ title, description, children, footer }: AuthLayoutProps) {
  return (
    <div className="grid min-h-dvh lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)]">
      <div className="flex flex-col">
        <header className="flex items-center justify-between px-6 py-5 sm:px-10">
          <Link
            to="/"
            className="rounded-lg focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            <Logo />
          </Link>
          <ThemeToggle />
        </header>

        <main className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center px-6 pt-6 pb-16">
          <div className="animate-rise">
            <h1 className="text-3xl font-semibold tracking-tight">{title}</h1>
            {description ? (
              <p className="mt-2 text-[15px] text-muted-foreground">{description}</p>
            ) : null}
            <div className="mt-8">{children}</div>
            {footer ? <div className="mt-8 text-sm text-muted-foreground">{footer}</div> : null}
          </div>
        </main>
      </div>

      <aside
        aria-hidden
        className="dark relative m-3 hidden overflow-hidden rounded-3xl bg-background text-foreground ring-1 ring-border lg:block"
      >
        <div className="glow-field animate-drift opacity-90" />
        <div className="dot-grid absolute inset-0 opacity-40" />
        <div className="relative flex h-full flex-col justify-end p-12">
          <p className="font-display text-5xl leading-[1.05] tracking-tight">
            Ask anything.
            <br />
            <span className="italic opacity-80">Check everything.</span>
          </p>
          <ul className="mt-10 space-y-4">
            {HIGHLIGHTS.map(({ icon: Icon, text }) => (
              <li key={text} className="flex items-center gap-3 text-[15px] text-foreground/85">
                <span className="flex size-9 items-center justify-center rounded-xl bg-white/10 ring-1 ring-white/15">
                  <Icon className="size-4" />
                </span>
                {text}
              </li>
            ))}
          </ul>
        </div>
      </aside>
    </div>
  )
}
