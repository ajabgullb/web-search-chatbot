import { expect, test } from '@playwright/test'

import { ask, register, startConversation } from './helpers.ts'

test("one user cannot open another user's conversation", async ({ page, browser }) => {
  await register(page, 'owner')
  await startConversation(page)
  await ask(page, 'hello')
  await expect(page.getByText(/Ask me anything/)).toBeVisible()
  const privateUrl = page.url()

  // A separate browser context: its own cookies, its own account.
  const strangerContext = await browser.newContext()
  const stranger = await strangerContext.newPage()
  await register(stranger, 'stranger')
  await stranger.goto(privateUrl)

  // A guessed or leaked id looks exactly like one that never existed.
  await expect(stranger.getByText('Conversation not found')).toBeVisible()
  await expect(stranger.getByText('hello', { exact: true })).toHaveCount(0)
  await expect(
    stranger.getByRole('navigation', { name: 'Conversations' }).getByText('No conversations yet.'),
  ).toBeVisible()
  await strangerContext.close()
})
