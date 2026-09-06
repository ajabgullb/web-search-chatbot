import { useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'

import { ApiError } from '@/api/errors'
import { AuthLayout } from '@/components/AuthLayout'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Field } from '@/components/ui/Field'
import { resetPassword } from '@/features/auth/api'
import { PASSWORD_MIN_LENGTH, validatePassword } from '@/features/auth/validation'

export function ResetPasswordPage() {
  // The token arrives in the link the server emailed:
  //   {FRONTEND_ORIGIN}/reset-password?token=...
  const [searchParams] = useSearchParams()
  const token = searchParams.get('token') ?? ''
  const navigate = useNavigate()

  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  if (!token) {
    return (
      <AuthLayout title="Reset link is incomplete">
        <Alert>
          This link is missing its token. Request a new one and use the most recent email.
        </Alert>
        <p className="mt-6 text-sm">
          <Link to="/forgot-password" className="underline underline-offset-4">
            Request a new link
          </Link>
        </p>
      </AuthLayout>
    )
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)

    const errors: Record<string, string> = {}
    const passwordError = validatePassword(password)
    if (passwordError) errors.password = passwordError
    if (password !== confirmation) errors.confirmation = 'Passwords do not match.'

    setFieldErrors(errors)
    if (Object.keys(errors).length > 0) return

    setSubmitting(true)
    try {
      await resetPassword({ token, password })
      // The reset revoked every session, so there is nothing to restore —
      // send them to sign in with the new password.
      void navigate('/login', { replace: true })
    } catch (error) {
      if (error instanceof ApiError && error.is('invalid_token')) {
        setFormError('This link has expired or has already been used. Request a new one.')
      } else if (error instanceof Error) {
        setFormError(error.message)
      } else {
        setFormError('Could not reset the password. Try again.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AuthLayout title="Choose a new password" description="You'll sign in again once it's saved.">
      <form onSubmit={(e) => void onSubmit(e)} noValidate className="space-y-4">
        {formError ? <Alert>{formError}</Alert> : null}

        <Field
          label="New password"
          type="password"
          name="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          error={fieldErrors.password}
          hint={`At least ${PASSWORD_MIN_LENGTH} characters.`}
          required
        />
        <Field
          label="Confirm new password"
          type="password"
          name="confirmation"
          autoComplete="new-password"
          value={confirmation}
          onChange={(e) => setConfirmation(e.target.value)}
          error={fieldErrors.confirmation}
          required
        />

        <Button type="submit" className="w-full" disabled={submitting}>
          {submitting ? 'Saving…' : 'Save new password'}
        </Button>
      </form>
    </AuthLayout>
  )
}
