import { MailCheck } from 'lucide-react'
import { useState } from 'react'

import { ApiError } from '@/api/errors'
import { Button } from '@/components/ui/Button'
import { resendVerification } from '@/features/auth/api'
import { useAuth } from '@/features/auth/useAuth'

type ResendState = 'idle' | 'sending' | 'sent' | 'error'

/**
 * Shown above the chat while the account's email is unconfirmed.
 *
 * The server refuses runs from unverified accounts regardless; this only
 * explains why before the user types a question and hits that wall.
 */
export function VerifyEmailBanner() {
  const { user } = useAuth()
  const [state, setState] = useState<ResendState>('idle')
  const [error, setError] = useState<string | null>(null)

  if (!user || user.is_verified) return null

  async function resend() {
    setState('sending')
    setError(null)
    try {
      await resendVerification()
      setState('sent')
    } catch (caught) {
      setState('error')
      setError(
        caught instanceof ApiError && caught.is('rate_limited')
          ? 'Too many emails requested. Wait a few minutes, then try again.'
          : 'The email could not be sent. Try again shortly.',
      )
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-primary/20 bg-primary/8 px-4 py-2 text-sm">
      <span className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-primary/15 text-primary">
        <MailCheck className="size-4" aria-hidden />
      </span>
      <p className="min-w-0 flex-1">
        Confirm your email to start chatting. We sent a link to{' '}
        <span className="font-medium">{user.email}</span>.
      </p>
      <Button
        size="sm"
        variant="outline"
        disabled={state === 'sending' || state === 'sent'}
        onClick={() => void resend()}
      >
        {state === 'sent' ? 'Email sent' : state === 'sending' ? 'Sending…' : 'Resend email'}
      </Button>
      {/* Always mounted, so the outcome is announced when it appears. */}
      <p role="status" className="w-full text-muted-foreground empty:hidden">
        {state === 'sent' ? 'Check your inbox for a new link.' : (error ?? '')}
      </p>
    </div>
  )
}
