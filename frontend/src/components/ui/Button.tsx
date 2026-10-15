import { cva, type VariantProps } from 'class-variance-authority'
import type { ComponentProps } from 'react'

import { cn } from '@/lib/utils'

const button = cva(
  // A visible focus ring is a WCAG 2.1 AA requirement, not decoration.
  'inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors ' +
    'focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background ' +
    'focus-visible:outline-none disabled:pointer-events-none disabled:opacity-60 ' +
    'motion-reduce:transition-none',
  {
    variants: {
      variant: {
        primary: 'bg-primary text-primary-foreground hover:bg-primary/90',
        outline: 'border border-border hover:bg-accent hover:text-accent-foreground',
        ghost: 'hover:bg-accent hover:text-accent-foreground',
      },
      size: {
        md: 'h-10 px-4 py-2',
        sm: 'h-9 px-3',
        icon: 'size-9',
      },
    },
    defaultVariants: { variant: 'primary', size: 'md' },
  },
)

// `ComponentProps` rather than the bare HTML attributes so `ref` passes through
// (React 19 treats it as an ordinary prop): callers move focus onto buttons.
type ButtonProps = ComponentProps<'button'> & VariantProps<typeof button>

export function Button({ className, variant, size, ...props }: ButtonProps) {
  return <button className={cn(button({ variant, size }), className)} {...props} />
}
