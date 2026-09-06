import { cn } from '@/lib/utils'

interface AlertProps {
  variant?: 'error' | 'success' | 'info'
  title?: string
  children: React.ReactNode
  className?: string
}

/**
 * Form- and page-level feedback.
 *
 * An error interrupts (`role="alert"`, assertive); anything else waits for a
 * pause (`role="status"`, polite). Announcing an informational note assertively
 * would cut across whatever a screen-reader user was already listening to.
 */
export function Alert({ variant = 'error', title, children, className }: AlertProps) {
  const styles = {
    error: 'border-destructive/40 bg-destructive/10 text-destructive',
    success: 'border-border bg-muted text-foreground',
    info: 'border-border bg-muted text-foreground',
  }[variant]

  return (
    <div
      role={variant === 'error' ? 'alert' : 'status'}
      className={cn('rounded-md border px-3 py-2 text-sm', styles, className)}
    >
      {title ? <p className="font-medium">{title}</p> : null}
      {children}
    </div>
  )
}
