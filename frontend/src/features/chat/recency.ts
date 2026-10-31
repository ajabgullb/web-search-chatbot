/**
 * Sidebar sections: conversations bucketed by when they were last active.
 *
 * Pure (the clock is an argument) so the boundaries are testable. Buckets are
 * calendar days in the user's own time zone, not 24-hour windows: something
 * from 11 pm is "Yesterday" at 1 am, which is what people mean by it.
 */

export type RecencyBucket = 'Today' | 'Yesterday' | 'Previous 7 days' | 'Previous 30 days' | 'Older'

const ORDER: readonly RecencyBucket[] = [
  'Today',
  'Yesterday',
  'Previous 7 days',
  'Previous 30 days',
  'Older',
]

const DAY_MS = 24 * 60 * 60 * 1000

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}

export function recencyBucket(iso: string, now: Date): RecencyBucket {
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return 'Older'
  // Rounded: a day with a daylight-saving change is 23 or 25 hours long.
  const days = Math.round((startOfDay(now) - startOfDay(then)) / DAY_MS)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return 'Previous 7 days'
  if (days < 30) return 'Previous 30 days'
  return 'Older'
}

export interface RecencyGroup<T> {
  label: RecencyBucket
  items: T[]
}

/**
 * Group items into sections, newest section first, keeping the order the
 * server returned within each section.
 */
export function groupByRecency<T extends { updated_at: string }>(
  items: readonly T[],
  now: Date,
): RecencyGroup<T>[] {
  const groups = new Map<RecencyBucket, T[]>()
  for (const item of items) {
    const label = recencyBucket(item.updated_at, now)
    const bucket = groups.get(label)
    if (bucket) bucket.push(item)
    else groups.set(label, [item])
  }
  return ORDER.filter((label) => groups.has(label)).map((label) => ({
    label,
    items: groups.get(label) ?? [],
  }))
}
