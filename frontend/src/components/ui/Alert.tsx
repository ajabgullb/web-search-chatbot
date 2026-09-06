import { cn } from '@/lib/utils'

interface AlertProps {
  variant?: 'error' | 'success'
  children: React.ReactNode
  className?: string
}

/**
 * Form-level feedback.
 *
 * `role="alert"` makes it an ARIA live region, so a failed sign-in is
 * announced instead of silently appearing above the form.
 */
export function Alert({ variant = 'error', children, className }: AlertProps) {
  return (
    <div
      role="alert"
      className={cn(
        'rounded-md border px-3 py-2 text-sm',
        variant === 'error'
          ? 'border-destructive/40 bg-destructive/10 text-destructive'
          : 'border-border bg-muted text-foreground',
        className,
      )}
    >
      {children}
    </div>
  )
}
