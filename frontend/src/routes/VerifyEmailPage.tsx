import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { ApiError } from '@/api/errors'
import { AuthLayout } from '@/components/AuthLayout'
import { Alert } from '@/components/ui/Alert'
import { verifyEmail } from '@/features/auth/api'
import { useAuth } from '@/features/auth/useAuth'

type Outcome = 'verifying' | 'verified' | 'invalid' | 'failed'

/**
 * `/verify-email?token=...`, the link from the confirmation email.
 *
 * Public, like `/reset-password`: the link may be opened in a browser that is
 * signed out, or signed in as someone else. The token alone is the proof.
 */
export function VerifyEmailPage() {
  const [searchParams] = useSearchParams()
  const token = searchParams.get('token') ?? ''
  const { status, refreshUser } = useAuth()
  const [outcome, setOutcome] = useState<Outcome>('verifying')
  // One request per token, shared across StrictMode's double mount, in the
  // same way the auth bootstrap shares its refresh.
  const attempt = useRef<{ token: string; result: Promise<Outcome> } | null>(null)

  useEffect(() => {
    if (!token) return
    if (attempt.current?.token !== token) {
      attempt.current = {
        token,
        result: verifyEmail({ token }).then(
          (): Outcome => 'verified',
          (error: unknown): Outcome =>
            error instanceof ApiError && error.is('invalid_token') ? 'invalid' : 'failed',
        ),
      }
    }
    let active = true
    void attempt.current.result.then((result) => {
      if (active) setOutcome(result)
    })
    return () => {
      active = false
    }
  }, [token])

  // The session may still be restoring when the verification lands, or the
  // other way round; whichever finishes second triggers the re-read, so the
  // chat screen drops its "confirm your email" prompt without a reload.
  useEffect(() => {
    if (outcome === 'verified' && status === 'authenticated') void refreshUser().catch(() => {})
  }, [outcome, status, refreshUser])

  if (!token) {
    return (
      <AuthLayout title="Confirmation link is incomplete">
        <Alert>This link is missing its token. Open the most recent email we sent you.</Alert>
      </AuthLayout>
    )
  }

  const next =
    status === 'authenticated' ? (
      <Link to="/chat" className="underline underline-offset-4">
        Start chatting
      </Link>
    ) : (
      <Link to="/login" className="underline underline-offset-4">
        Sign in
      </Link>
    )

  if (outcome === 'verifying') {
    return (
      <AuthLayout title="Confirming your email">
        <p role="status" className="text-sm text-muted-foreground">
          One moment…
        </p>
      </AuthLayout>
    )
  }

  if (outcome === 'verified') {
    return (
      <AuthLayout title="Email confirmed">
        <Alert variant="success">Thanks. You can now send messages.</Alert>
        <p className="mt-6 text-sm">{next}</p>
      </AuthLayout>
    )
  }

  return (
    <AuthLayout title="Could not confirm your email">
      <Alert>
        {outcome === 'invalid'
          ? 'This link has expired or is not valid. Sign in and ask for a new one from the chat screen.'
          : 'Something went wrong. Try opening the link again in a moment.'}
      </Alert>
      <p className="mt-6 text-sm">{next}</p>
    </AuthLayout>
  )
}
