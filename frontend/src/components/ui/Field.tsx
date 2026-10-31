import { useId } from 'react'
import type { InputHTMLAttributes, ReactNode } from 'react'

import { cn } from '@/lib/utils'

interface FieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'id'> {
  label: string
  error?: string | undefined
  hint?: ReactNode
}

/**
 * A labelled input with its error wired up for screen readers.
 *
 * `aria-describedby` points at the message and `aria-invalid` marks the state,
 * so the error is announced rather than merely being red — colour alone would
 * fail WCAG 1.4.1.
 */
export function Field({ label, error, hint, className, ...props }: FieldProps) {
  const id = useId()
  const errorId = `${id}-error`
  const hintId = `${id}-hint`
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(' ')

  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      <input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy || undefined}
        className={cn(
          'h-11 w-full rounded-xl border border-input bg-card px-3.5 text-sm shadow-xs',
          'transition-[border-color,box-shadow] duration-150 motion-reduce:transition-none',
          'placeholder:text-muted-foreground/70 hover:border-ring/50',
          'focus-visible:border-ring focus-visible:ring-4 focus-visible:ring-ring/20 focus-visible:outline-none',
          error && 'border-destructive focus-visible:ring-destructive/20',
          className,
        )}
        {...props}
      />
      {hint ? (
        <p id={hintId} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className="animate-fade text-sm text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  )
}
