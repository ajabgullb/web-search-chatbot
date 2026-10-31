import type { ComponentProps } from 'react'

import { button, type ButtonVariants } from '@/components/ui/buttonVariants'
import { cn } from '@/lib/utils'

// `ComponentProps` rather than the bare HTML attributes so `ref` passes through
// (React 19 treats it as an ordinary prop): callers move focus onto buttons.
type ButtonProps = ComponentProps<'button'> & ButtonVariants

export function Button({ className, variant, size, ...props }: ButtonProps) {
  return <button className={cn(button({ variant, size }), className)} {...props} />
}
