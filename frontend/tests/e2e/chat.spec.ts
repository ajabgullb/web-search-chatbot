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

test('only the thread scrolls, never the page around it', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  await register(page, 'scroll')
  await startConversation(page)
  // Enough cited turns to overflow the thread several times over. Each source
  // card holds visually hidden (absolutely positioned) labels, which is what
  // used to stretch the page.
  for (let turn = 1; turn <= 3; turn++) {
    await ask(page, CITED_QUESTION)
    await expect(page.getByRole('region', { name: 'Sources' })).toHaveCount(turn)
    await expect(page.getByRole('button', { name: 'Send message' })).toBeVisible()
  }

  const thread = page.getByTestId('thread')
  expect(await thread.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true)
  const pageOverflow = await page.evaluate(
    () => document.documentElement.scrollHeight - window.innerHeight,
  )
  expect(pageOverflow).toBeLessThanOrEqual(0)
})

test('the sidebar collapses on desktop and stays collapsed after a reload', async ({ page }) => {
  await register(page, 'collapse')
  const list = page.getByRole('navigation', { name: 'Conversations' })
  const greeting = page.getByRole('heading', { level: 1 })
  await expect(greeting).toContainText('E2E')
  const xWithSidebar = (await greeting.boundingBox())?.x ?? 0

  await page.getByRole('button', { name: 'Collapse sidebar' }).click()
  await expect(list).toBeHidden()
  // Focus follows the toggle rather than falling to <body>.
  const expand = page.getByRole('button', { name: 'Expand sidebar' })
  await expect(expand).toBeFocused()
  // The freed space goes to the chat.
  expect((await greeting.boundingBox())?.x ?? 0).toBeLessThan(xWithSidebar)

  await page.reload()
  await expect(greeting).toBeVisible()
  await expect(list).toBeHidden()

  await expand.click()
  await expect(list).toBeVisible()
  await expect(page.getByRole('button', { name: 'Collapse sidebar' })).toBeFocused()
})

test('a question asked on the chat home opens a new conversation already answering it', async ({
  page,
}) => {
  await register(page, 'home')

  // The home box is focused on arrival: typing is all the page is for.
  const box = page.getByRole('textbox', { name: 'Message', exact: true })
  await expect(box).toBeFocused()
  await box.fill(CITED_QUESTION)
  await box.press('Enter')

  await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)
  await expect(page.getByLabel('Your message')).toContainText(CITED_QUESTION)
  await expect(page.getByText(CITED_ANSWER_TEXT)).toBeVisible()
  // Asked once, so stored once: the optimistic copy was reconciled.
  await expect(page.getByLabel('Your message')).toHaveCount(1)

  // A suggestion is a question too, in a conversation of its own.
  await page.goto('/chat')
  const suggestion = page.getByRole('list', { name: 'Suggestions' }).getByRole('button').first()
  const prompt = (await suggestion.locator('span > span').last().textContent()) ?? ''
  await suggestion.click()
  await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)
  await expect(page.getByLabel('Your message')).toContainText(prompt)
  await expect(page.getByRole('article', { name: 'Assistant message' })).toBeVisible()
  await expect(
    page.getByRole('navigation', { name: 'Conversations' }).getByRole('link'),
  ).toHaveCount(2)
})
