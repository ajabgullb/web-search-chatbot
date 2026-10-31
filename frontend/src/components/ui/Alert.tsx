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
    error: 'border-destructive/30 bg-destructive/8 text-destructive',
    success: 'border-success/30 bg-success/10 text-foreground',
    info: 'border-primary/20 bg-primary/6 text-foreground',
  }[variant]

  return (
    <div
      role={variant === 'error' ? 'alert' : 'status'}
      className={cn('animate-fade rounded-xl border px-3.5 py-2.5 text-sm', styles, className)}
    >
      {title ? <p className="font-medium">{title}</p> : null}
      {children}
    </div>
  )
}
