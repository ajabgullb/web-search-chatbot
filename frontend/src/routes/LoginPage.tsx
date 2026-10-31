import { useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'

import { ApiError } from '@/api/errors'
import { AuthLayout } from '@/components/AuthLayout'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Field } from '@/components/ui/Field'
import { useAuth } from '@/features/auth/useAuth'
import { collect, validateEmail, validatePassword } from '@/features/auth/validation'
import type { FieldErrors } from '@/features/auth/validation'

interface LocationState {
  from?: { pathname: string }
}

export function LoginPage() {
  const { signIn } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({})
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // Where the guard wanted to send them before the redirect to /login.
  const destination = (location.state as LocationState | null)?.from?.pathname ?? '/chat'

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)

    const errors = collect({
      email: validateEmail(email),
      password: validatePassword(password),
    })
    setFieldErrors(errors)
    if (Object.keys(errors).length > 0) return

    setSubmitting(true)
    try {
      await signIn(email, password)
      void navigate(destination, { replace: true })
    } catch (error) {
      if (error instanceof ApiError && error.is('rate_limited')) {
        setFormError('Too many attempts. Wait a minute and try again.')
      } else if (error instanceof Error) {
        setFormError(error.message)
      } else {
        setFormError('Sign in failed. Try again.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AuthLayout
      title="Sign in"
      description="Welcome back."
      footer={
        <p>
          No account?{' '}
          <Link
            to="/register"
            className="font-medium text-foreground underline decoration-foreground/30 underline-offset-4 hover:decoration-foreground"
          >
            Create one
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
          error={fieldErrors.email}
          required
        />
        <Field
          label="Password"
          type="password"
          name="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          error={fieldErrors.password}
          required
        />

        <div className="-mt-1 flex justify-end">
          <Link
            to="/forgot-password"
            className="text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:underline"
          >
            Forgot password?
          </Link>
        </div>
        <Button type="submit" variant="ink" size="lg" className="w-full" disabled={submitting}>
          {submitting ? 'Signing in…' : 'Sign in'}
        </Button>
      </form>
    </AuthLayout>
  )
}
