import { expect, type Page } from '@playwright/test'

export const PASSWORD = 'correct-horse-battery-staple'

/** Questions the scripted backend model recognises (backend/tests/e2e_server.py). */
export const CITED_QUESTION = 'What is the latest central bank rate decision?'
export const SLOW_QUESTION = 'Please write a slow, detailed essay about gardening.'
export const CITED_ANSWER_TEXT = 'held its policy rate at 5.25%'
export const FIRST_SOURCE = 'Central bank holds rates'

export function uniqueEmail(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
}

/** Register a fresh account through the UI; resolves on the chat home. */
export async function register(page: Page, prefix = 'user'): Promise<string> {
  const email = uniqueEmail(prefix)
  await page.goto('/register')
  await page.getByLabel('Display name').fill(`E2E ${prefix}`)
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password', { exact: true }).fill(PASSWORD)
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page).toHaveURL(/\/chat$/)
  return email
}

export async function signIn(page: Page, email: string): Promise<void> {
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password', { exact: true }).fill(PASSWORD)
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
}

/** Start a conversation from the chat home; resolves on its page. */
export async function startConversation(page: Page): Promise<void> {
  await page.getByRole('main').getByRole('button', { name: 'New chat' }).click()
  await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)
}

export async function ask(page: Page, question: string): Promise<void> {
  const composer = page.getByRole('textbox', { name: 'Message', exact: true })
  await composer.fill(question)
  await composer.press('Enter')
}
