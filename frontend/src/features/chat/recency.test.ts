import { describe, expect, it } from 'vitest'

import { groupByRecency, recencyBucket } from '@/features/chat/recency'

// Local time on purpose: buckets are calendar days in the user's time zone.
const now = new Date(2026, 9, 31, 1, 0) // 31 Oct 2026, 1 am

function at(year: number, month: number, day: number, hour = 12): string {
  return new Date(year, month, day, hour).toISOString()
}

describe('recencyBucket', () => {
  it('uses calendar days, not 24-hour windows', () => {
    expect(recencyBucket(at(2026, 9, 31, 0), now)).toBe('Today')
    // Two hours ago, but before midnight.
    expect(recencyBucket(at(2026, 9, 30, 23), now)).toBe('Yesterday')
  })

  it('places each age in its section', () => {
    expect(recencyBucket(at(2026, 9, 26), now)).toBe('Previous 7 days')
    expect(recencyBucket(at(2026, 9, 10), now)).toBe('Previous 30 days')
    expect(recencyBucket(at(2026, 5, 1), now)).toBe('Older')
  })

  it('treats a future or unreadable timestamp safely', () => {
    expect(recencyBucket(at(2026, 10, 2), now)).toBe('Today')
    expect(recencyBucket('not a date', now)).toBe('Older')
  })
})

describe('groupByRecency', () => {
  it('orders sections newest first and keeps the server order inside each', () => {
    const items = [
      { id: 'a', updated_at: at(2026, 9, 31, 0) },
      { id: 'old', updated_at: at(2026, 0, 1) },
      { id: 'b', updated_at: at(2026, 9, 30, 9) },
      { id: 'c', updated_at: at(2026, 9, 31, 0) },
    ]
    const groups = groupByRecency(items, now)
    expect(groups.map((g) => g.label)).toEqual(['Today', 'Yesterday', 'Older'])
    expect(groups[0]?.items.map((i) => i.id)).toEqual(['a', 'c'])
  })

  it('returns no sections for no items', () => {
    expect(groupByRecency([], now)).toEqual([])
  })
})
