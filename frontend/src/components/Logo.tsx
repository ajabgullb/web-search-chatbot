import { useId } from 'react'

import { cn } from '@/lib/utils'

/**
 * The brand mark: a lens with a spark in it — search, and the answer it
 * turns up. Drawn inline so it inverts with the theme, and decorative
 * wherever a text name sits beside it.
 */
export function LogoMark({ className }: { className?: string }) {
  // Unique per instance: two logos on one page must not share gradient ids.
  const gradient = useId()
  return (
    <svg viewBox="0 0 32 32" aria-hidden className={cn('size-8 shrink-0', className)}>
      <defs>
        <linearGradient id={gradient} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="var(--logo-1)" />
          <stop offset="1" stopColor="var(--logo-2)" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill={`url(#${gradient})`} />
      <circle cx="14.5" cy="14.5" r="6.5" fill="none" stroke="var(--logo-ink)" strokeWidth="2.4" />
      <path d="M19.4 19.4 24 24" stroke="var(--logo-ink)" strokeWidth="2.6" strokeLinecap="round" />
      <path
        d="M14.5 10.6c.35 2.1 1.8 3.55 3.9 3.9-2.1.35-3.55 1.8-3.9 3.9-.35-2.1-1.8-3.55-3.9-3.9 2.1-.35 3.55-1.8 3.9-3.9Z"
        fill="var(--logo-ink)"
      />
    </svg>
  )
}

/** Mark and name together, for headers. */
export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn('inline-flex items-center gap-2.5', className)}>
      <LogoMark className="size-7" />
      <span className="text-[15px] font-semibold tracking-tight">Web Search Chatbot</span>
    </span>
  )
}
