import { expect, test } from '@playwright/test'

import { ask, openConfirmationLink, register, signIn, startConversation } from './helpers.ts'

test('register, keep the session across a reload, sign out, sign back in', async ({ page }) => {
  const email = await register(page, 'auth')
  const account = page.getByRole('navigation', { name: 'Conversations' })
  await expect(account.getByText(email)).toBeVisible()

  // The access token lives in memory only; a reload restores the session
  // through the refresh cookie.
  await page.reload()
  await expect(page).toHaveURL(/\/chat$/)
  await expect(account.getByText(email)).toBeVisible()

  await account.getByRole('button', { name: 'Sign out' }).click()
  await expect(page).toHaveURL(/\/login$/)

  // Signed out means signed out on the server too: the refresh cookie no
  // longer restores anything.
  await page.goto('/chat')
  await expect(page).toHaveURL(/\/login$/)

  await signIn(page, email)
  await expect(page).toHaveURL(/\/chat$/)
  await expect(account.getByText(email)).toBeVisible()
})

test('a guarded page sends a visitor to sign in, then back where they were going', async ({
  page,
}) => {
  const email = await register(page, 'guard')
  await startConversation(page)
  const destination = page.url()
  await page
    .getByRole('navigation', { name: 'Conversations' })
    .getByRole('button', { name: 'Sign out' })
    .click()
  await expect(page).toHaveURL(/\/login$/)

  // A specific conversation, not /chat: /chat is where sign-in lands anyway.
  await page.goto(destination)
  await expect(page).toHaveURL(/\/login$/)
  await signIn(page, email)
  await expect(page).toHaveURL(destination)
})

test('an unconfirmed account is asked to confirm, and can chat once it has', async ({ page }) => {
  const email = await register(page, 'confirm', { verify: false })
  const prompt = page.getByText(/confirm your email to start chatting/i)
  await expect(prompt).toBeVisible()

  // The server refuses the question; what was typed is kept for a retry.
  await startConversation(page)
  await ask(page, 'Hello there')
  await expect(page.getByText(/^not sent\. confirm your email address/i)).toBeVisible()

  await openConfirmationLink(page, email)
  await page.getByRole('link', { name: 'Start chatting' }).click()
  await expect(page).toHaveURL(/\/chat$/)
  await expect(prompt).toBeHidden()
})
