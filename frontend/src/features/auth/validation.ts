/**
 * Client-side form validation.
 *
 * Deliberately a mirror of the server's rules, not a replacement for them.
 * Its job is fast feedback; the server remains the authority, and any rule it
 * enforces that this file misses still comes back as a 422 the forms display.
 */

/** Matches the backend's `password_min_length`. */
export const PASSWORD_MIN_LENGTH = 12

export type FieldErrors = Record<string, string>

export function validateEmail(value: string): string | undefined {
  if (!value.trim()) return 'Enter your email address.'
  // Intentionally loose: the server and the mail system are the real judges of
  // deliverability, and an over-strict pattern rejects valid addresses.
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) return 'Enter a valid email address.'
  return undefined
}

export function validatePassword(value: string): string | undefined {
  if (!value) return 'Enter a password.'
  if (value.length < PASSWORD_MIN_LENGTH) {
    return `Use at least ${PASSWORD_MIN_LENGTH} characters.`
  }
  return undefined
}

export function validateDisplayName(value: string): string | undefined {
  if (!value.trim()) return 'Enter a display name.'
  if (value.trim().length > 100) return 'Use 100 characters or fewer.'
  return undefined
}

export function collect(entries: Record<string, string | undefined>): FieldErrors {
  const errors: FieldErrors = {}
  for (const [field, message] of Object.entries(entries)) {
    if (message) errors[field] = message
  }
  return errors
}
