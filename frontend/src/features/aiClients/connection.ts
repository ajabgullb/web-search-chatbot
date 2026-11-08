/**
 * What a client needs to connect: the endpoint URL and ready-to-paste
 * configuration for the new token (ADR-0019).
 */

import type { TokenScope } from '@/api/types'

export const SERVER_NAME = 'web-search-chatbot'

export const SCOPES: { value: TokenScope; label: string; hint: string }[] = [
  {
    value: 'conversations:read',
    label: 'Read your conversations and documents',
    hint: 'List conversations, read their messages, and search their PDFs.',
  },
  {
    value: 'runs:create',
    label: 'Ask questions',
    hint: 'Each question uses one message of your daily allowance and is saved to your history.',
  },
]

export const LIFETIMES = [7, 30, 90] as const

/** The endpoint, on whatever origin served this page (nginx or Vite proxy it to the API). */
export function mcpUrl(): string {
  return `${window.location.origin}/api/v1/mcp`
}

export function claudeCodeCommand(url: string, token: string): string {
  return `claude mcp add --transport http ${SERVER_NAME} ${url} --header "Authorization: Bearer ${token}"`
}

export function jsonConfig(url: string, token: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        [SERVER_NAME]: { type: 'http', url, headers: { Authorization: `Bearer ${token}` } },
      },
    },
    null,
    2,
  )
}
