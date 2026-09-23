/**
 * Link-safety checks. AGENTS.md §6: only http and https are ever rendered as
 * links, and every outbound link opens with `rel="noopener noreferrer"`.
 */

/** A link inside rendered Markdown. Relative links resolve against this site. */
export function isSafeHref(href: string | undefined): href is string {
  if (!href) return false
  try {
    const url = new URL(href, window.location.origin)
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

/** A source URL from search. Must be absolute: a relative one is not a source. */
export function isSafeExternalUrl(href: string): boolean {
  try {
    const url = new URL(href)
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}
