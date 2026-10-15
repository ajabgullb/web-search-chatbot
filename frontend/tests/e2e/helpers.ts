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

/** The scripted API server; must match API_PORT in playwright.config.ts. */
const API_ORIGIN = 'http://127.0.0.1:8010'

/**
 * Register a fresh account through the UI; resolves on the chat home.
 *
 * Confirms the email too, unless told not to: only a confirmed account may
 * send messages, and most specs are about what happens after that.
 */
export async function register(
  page: Page,
  prefix = 'user',
  { verify = true }: { verify?: boolean } = {},
): Promise<string> {
  const email = uniqueEmail(prefix)
  await page.goto('/register')
  await page.getByLabel('Display name').fill(`E2E ${prefix}`)
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password', { exact: true }).fill(PASSWORD)
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page).toHaveURL(/\/chat$/)
  if (verify) {
    await openConfirmationLink(page, email)
    await page.getByRole('link', { name: 'Start chatting' }).click()
    await expect(page).toHaveURL(/\/chat$/)
  }
  return email
}

/**
 * Follow the link in the latest email sent to `email`, as the user would.
 * The e2e server keeps sent mail in memory (backend/tests/e2e_server.py).
 */
export async function openConfirmationLink(page: Page, email: string): Promise<void> {
  const response = await page.request.get(`${API_ORIGIN}/__e2e/latest-email`, {
    params: { to: email },
  })
  expect(response.ok()).toBe(true)
  const { body } = (await response.json()) as { body: string }
  const link = /https?:\/\/\S+\/verify-email\?token=\S+/.exec(body)?.[0]
  expect(link, 'confirmation email carries a link').toBeTruthy()
  await page.goto(link ?? '')
  await expect(page.getByRole('heading', { name: 'Email confirmed' })).toBeVisible()
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
