import { ArrowLeft } from 'lucide-react'
import { Link } from 'react-router'

import { buttonClasses } from '@/components/ui/buttonVariants'

export function NotFoundPage() {
  return (
    <main className="relative isolate mx-auto flex min-h-dvh max-w-lg flex-col items-center justify-center gap-4 overflow-hidden px-6 text-center">
      <div aria-hidden className="absolute inset-0 -z-10">
        <div className="glow-field opacity-50" />
      </div>
      <p aria-hidden className="text-gradient font-display text-8xl leading-none italic">
        404
      </p>
      <h1 className="text-3xl font-semibold tracking-tight">Page not found</h1>
      <p className="text-muted-foreground">
        That page does not exist, or you do not have access to it.
      </p>
      <Link to="/" className={buttonClasses({ variant: 'outline', className: 'mt-2' })}>
        <ArrowLeft className="size-4" aria-hidden />
        Back to the home page
      </Link>
    </main>
  )
}
