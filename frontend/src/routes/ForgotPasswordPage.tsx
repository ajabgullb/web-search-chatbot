import { useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router'

import { ApiError } from '@/api/errors'
import { AuthLayout } from '@/components/AuthLayout'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Field } from '@/components/ui/Field'
import { forgotPassword } from '@/features/auth/api'
import { validateEmail } from '@/features/auth/validation'

export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [fieldError, setFieldError] = useState<string | undefined>(undefined)
  const [formError, setFormError] = useState<string | null>(null)
  const [sent, setSent] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)

    const error = validateEmail(email)
    setFieldError(error)
    if (error) return

    setSubmitting(true)
    try {
      await forgotPassword({ email })
      setSent(true)
    } catch (error) {
      if (error instanceof ApiError && error.is('rate_limited')) {
        setFormError('Too many requests. Wait a few minutes and try again.')
      } else {
        setFormError('Could not send the email. Try again shortly.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  // The confirmation deliberately does not reveal whether the address exists —
  // it mirrors the server, which answers identically either way.
  if (sent) {
    return (
      <AuthLayout title="Check your email">
        <Alert variant="success">
          If an account exists for {email}, a reset link is on its way. The link can be used once
          and expires in 30 minutes.
        </Alert>
        <p className="mt-6 text-sm text-muted-foreground">
          <Link
            to="/login"
            className="font-medium text-foreground underline decoration-foreground/30 underline-offset-4 hover:decoration-foreground"
          >
            Back to sign in
          </Link>
        </p>
      </AuthLayout>
    )
  }

  return (
    <AuthLayout
      title="Reset your password"
      description="We'll email you a link to choose a new one."
      footer={
        <p>
          Remembered it?{' '}
          <Link
            to="/login"
            className="font-medium text-foreground underline decoration-foreground/30 underline-offset-4 hover:decoration-foreground"
          >
            Sign in
          </Link>
        </p>
      }
    >
      <form onSubmit={(e) => void onSubmit(e)} noValidate className="space-y-4">
        {formError ? <Alert>{formError}</Alert> : null}

        <Field
          label="Email"
          type="email"
          name="email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          error={fieldError}
          required
        />

        <Button type="submit" variant="ink" size="lg" className="w-full" disabled={submitting}>
          {submitting ? 'Sending…' : 'Send reset link'}
        </Button>
      </form>
    </AuthLayout>
  )
}
