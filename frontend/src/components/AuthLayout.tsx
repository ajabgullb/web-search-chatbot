import { Link } from 'react-router'
import type { ReactNode } from 'react'

import { ThemeToggle } from '@/components/ThemeToggle'

interface AuthLayoutProps {
  title: string
  description?: ReactNode
  children: ReactNode
  footer?: ReactNode
}

/** Shared frame for the four unauthenticated auth pages. */
export function AuthLayout({ title, description, children, footer }: AuthLayoutProps) {
  return (
    <div className="min-h-dvh">
      <header className="flex items-center justify-between px-6 py-4">
        <Link to="/" className="text-sm font-semibold hover:underline underline-offset-4">
          Web Search Chatbot
        </Link>
        <ThemeToggle />
      </header>

      <main className="mx-auto w-full max-w-sm px-6 py-10">
        <h1 className="text-2xl font-semibold">{title}</h1>
        {description ? <p className="mt-2 text-sm text-muted-foreground">{description}</p> : null}
        <div className="mt-6">{children}</div>
        {footer ? <div className="mt-6 text-sm text-muted-foreground">{footer}</div> : null}
      </main>
    </div>
  )
}
