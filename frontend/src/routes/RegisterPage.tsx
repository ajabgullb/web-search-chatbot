import { useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'

import { ApiError } from '@/api/errors'
import { AuthLayout } from '@/components/AuthLayout'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Field } from '@/components/ui/Field'
import { useAuth } from '@/features/auth/useAuth'
import {
  PASSWORD_MIN_LENGTH,
  collect,
  validateDisplayName,
  validateEmail,
  validatePassword,
} from '@/features/auth/validation'
import type { FieldErrors } from '@/features/auth/validation'

export function RegisterPage() {
  const { signUp } = useAuth()
  const navigate = useNavigate()

  const [displayName, setDisplayName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)

    const errors = collect({
      display_name: validateDisplayName(displayName),
      email: validateEmail(email),
      password: validatePassword(password),
    })
    setFieldErrors(errors)
    if (Object.keys(errors).length > 0) return

    setSubmitting(true)
    try {
      await signUp(email, password, displayName)
      void navigate('/chat', { replace: true })
    } catch (error) {
      if (error instanceof ApiError && error.is('email_already_registered')) {
        setFieldErrors({ email: 'An account with that email already exists.' })
      } else if (error instanceof ApiError && error.is('validation_failed')) {
        // The server is the authority; surface its field messages verbatim.
        setFieldErrors(error.fieldErrors())
      } else if (error instanceof Error) {
        setFormError(error.message)
      } else {
        setFormError('Could not create the account. Try again.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AuthLayout
      title="Create an account"
      description="Ask questions and get answers with sources attached."
      footer={
        <p>
          Already have an account?{' '}
          <Link to="/login" className="underline underline-offset-4">
            Sign in
          </Link>
        </p>
      }
    >
      <form onSubmit={(e) => void onSubmit(e)} noValidate className="space-y-4">
        {formError ? <Alert>{formError}</Alert> : null}

        <Field
          label="Display name"
          name="display_name"
          autoComplete="name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          error={fieldErrors.display_name}
          required
        />
        <Field
          label="Email"
          type="email"
          name="email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          error={fieldErrors.email}
          required
        />
        <Field
          label="Password"
          type="password"
          name="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          error={fieldErrors.password}
          hint={`At least ${PASSWORD_MIN_LENGTH} characters. Longer passphrases are better.`}
          required
        />

        <Button type="submit" className="w-full" disabled={submitting}>
          {submitting ? 'Creating account…' : 'Create account'}
        </Button>
      </form>
    </AuthLayout>
  )
}
