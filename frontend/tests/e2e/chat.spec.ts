import { expect, test } from '@playwright/test'

import {
  ask,
  CITED_ANSWER_TEXT,
  CITED_QUESTION,
  FIRST_SOURCE,
  register,
  startConversation,
} from './helpers.ts'

test('a fresh-information question streams a cited answer that survives a reload', async ({
  page,
}) => {
  await register(page, 'chat')
  await startConversation(page)
  await ask(page, CITED_QUESTION)

  // Streamed, then settled: the answer text, an inline citation, and sources.
  await expect(page.getByText(CITED_ANSWER_TEXT)).toBeVisible()
  const sources = page.getByRole('region', { name: 'Sources' })
  await expect(sources).toBeVisible()
  const sourceLink = sources.getByRole('link', { name: `Source 1: ${FIRST_SOURCE}` })
  await expect(sourceLink).toHaveAttribute('href', 'https://news.example.com/rates')
  await expect(sourceLink).toHaveAttribute('rel', 'noopener noreferrer')
  await expect(sourceLink).toHaveAttribute('target', '_blank')
  await expect(page.getByRole('button', { name: 'Send message' })).toBeVisible()

  // An inline citation jumps to its source card.
  await page.getByRole('button', { name: `Source 1: ${FIRST_SOURCE}` }).click()
  await expect(sources.getByRole('listitem').filter({ hasText: FIRST_SOURCE })).toBeFocused()

  // The conversation is named from its first question and listed.
  const list = page.getByRole('navigation', { name: 'Conversations' })
  await expect(list.getByText(/latest central bank rate decision/)).toBeVisible()

  // Reload: history, answer, and citations come back from the server.
  await page.reload()
  await expect(page.getByLabel('Your message')).toContainText(CITED_QUESTION)
  await expect(page.getByText(CITED_ANSWER_TEXT)).toBeVisible()
  await expect(
    page.getByRole('region', { name: 'Sources' }).getByRole('link', { name: /Source 2/ }),
  ).toBeVisible()
  await expect(page.getByRole('button', { name: `Source 1: ${FIRST_SOURCE}` })).toBeVisible()
})
