import { describe, expect, it } from 'vitest'

import {
  firstName,
  GREETING_COUNT,
  greetingText,
  pickGreetingIndex,
} from '@/features/chat/greetings'

describe('the chat home greeting', () => {
  it('greets by the first word of the display name', () => {
    expect(firstName('  Ada   Lovelace ')).toBe('Ada')
    expect(firstName('Grace')).toBe('Grace')
    expect(firstName('   ')).toBeNull()
    expect(firstName(undefined)).toBeNull()
  })

  it('never repeats the previous greeting', () => {
    for (let previous = 0; previous < GREETING_COUNT; previous++) {
      // Sweep the random source across its whole range, including the edge.
      for (const r of [0, 0.2, 0.5, 0.8, 0.999999]) {
        const index = pickGreetingIndex(previous, () => r)
        expect(index).not.toBe(previous)
        expect(index).toBeGreaterThanOrEqual(0)
        expect(index).toBeLessThan(GREETING_COUNT)
      }
    }
  })

  it('can reach every greeting', () => {
    const seen = new Set<number>()
    for (let step = 0; step < GREETING_COUNT; step++) {
      seen.add(pickGreetingIndex(-1, () => step / GREETING_COUNT))
    }
    expect(seen.size).toBe(GREETING_COUNT)
  })

  it('names the time of day from the local clock', () => {
    // Index 0 is the time-of-day greeting.
    const at = (hour: number) => greetingText(0, 'Ada', new Date(2026, 0, 1, hour))
    expect(at(8)).toBe('Good morning, Ada')
    expect(at(14)).toBe('Good afternoon, Ada')
    expect(at(21)).toBe('Good evening, Ada')
    expect(at(2)).toBe('Good evening, Ada')
  })

  it('falls back to a nameless heading', () => {
    expect(greetingText(1, '', new Date())).toBe('What would you like to know?')
  })
})
