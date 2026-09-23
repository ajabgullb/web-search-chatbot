/**
 * A random v4 UUID, for idempotency keys and local placeholder ids.
 *
 * `crypto.randomUUID` exists only in secure contexts, so the dev server opened
 * over a LAN address on plain http would not have it. `getRandomValues` has no
 * such restriction and is just as random.
 */
export function randomId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()

  const bytes = crypto.getRandomValues(new Uint8Array(16))
  // RFC 9562: version 4 in the high nibble of byte 6, variant 10 in byte 8.
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
