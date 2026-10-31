import { cva, type VariantProps } from 'class-variance-authority'

import { cn } from '@/lib/utils'

/** Button styles, shared by `<Button>` and links that look like one. */
export const button = cva(
  // A visible focus ring is a WCAG 2.1 AA requirement, not decoration.
  'inline-flex items-center justify-center gap-2 rounded-xl text-sm font-medium ' +
    'transition-[background-color,color,box-shadow,transform] duration-150 active:scale-[0.97] ' +
    'focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background ' +
    'focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50 ' +
    'motion-reduce:transition-none motion-reduce:active:scale-100',
  {
    variants: {
      variant: {
        primary:
          'bg-primary text-primary-foreground shadow-sm shadow-primary/25 hover:bg-primary/90 hover:shadow-md hover:shadow-primary/30',
        // The filled call to action, kept for the main action on a screen.
        ink: 'bg-ink shadow-glow hover:opacity-90',
        outline: 'border border-border bg-card/60 hover:bg-accent hover:text-accent-foreground',
        ghost: 'hover:bg-accent hover:text-accent-foreground',
      },
      size: {
        md: 'h-10 px-4 py-2',
        sm: 'h-9 px-3',
        lg: 'h-12 px-6 text-[15px]',
        icon: 'size-9',
      },
    },
    defaultVariants: { variant: 'primary', size: 'md' },
  },
)

export type ButtonVariants = VariantProps<typeof button>

/** The same styles for a link that should look like a button. */
export function buttonClasses(options: ButtonVariants & { className?: string }) {
  const { className, ...variants } = options
  return cn(button(variants), className)
}
