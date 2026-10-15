/**
 * The chat home's greeting: the user's first name in one of several lines.
 *
 * Kept pure (the clock and the random source are arguments) so the choice is
 * testable; `ChatPage` supplies the real ones.
 */

type TimeOfDay = 'morning' | 'afternoon' | 'evening'

type Greeting = (name: string, timeOfDay: TimeOfDay) => string

const timeOfDayGreeting: Greeting = (name, timeOfDay) => `Good ${timeOfDay}, ${name}`

const GREETINGS: readonly Greeting[] = [
  timeOfDayGreeting,
  (name) => `Welcome back, ${name}`,
  (name) => `What's on your mind, ${name}?`,
  (name) => `Ready when you are, ${name}`,
  (name) => `What should we look up, ${name}?`,
  (name) => `Curious about something, ${name}?`,
  (name) => `Let's find some answers, ${name}`,
  (name) => `Hi ${name}, ask me anything`,
]

/** How many greetings there are; an index outside `0..count-1` means "none yet". */
export const GREETING_COUNT = GREETINGS.length

function timeOfDay(hour: number): TimeOfDay {
  if (hour >= 5 && hour < 12) return 'morning'
  if (hour >= 12 && hour < 18) return 'afternoon'
  return 'evening'
}

/**
 * The name to greet someone by: the first word of their display name.
 *
 * "Ada Lovelace" is greeted as "Ada". A blank name yields `null`.
 */
export function firstName(displayName: string | null | undefined): string | null {
  const first = displayName?.trim().split(/\s+/)[0]
  return first ? first : null
}

/**
 * Choose a greeting index different from `previous`, so moving between chats
 * and back never shows the same line twice in a row.
 */
export function pickGreetingIndex(previous: number, random: () => number = Math.random): number {
  // Draw from the other N-1 slots, then skip over `previous`. Uniform, and no
  // retry loop.
  const valid = previous >= 0 && previous < GREETING_COUNT
  const pool = valid ? GREETING_COUNT - 1 : GREETING_COUNT
  const draw = Math.min(Math.floor(random() * pool), pool - 1)
  return valid && draw >= previous ? draw + 1 : draw
}

export function greetingText(index: number, displayName: string | null | undefined, now: Date) {
  const name = firstName(displayName)
  // Without a name, a plain heading beats a line with a hole in it.
  if (!name) return 'What would you like to know?'
  const greeting = GREETINGS[index] ?? timeOfDayGreeting
  return greeting(name, timeOfDay(now.getHours()))
}
