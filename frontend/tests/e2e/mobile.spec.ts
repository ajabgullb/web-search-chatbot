import { expect, test } from '@playwright/test'

import { ask, register } from './helpers.ts'

// Runs in the "mobile" project (a Pixel 7 viewport); see playwright.config.ts.
test('the chat works one-handed on a phone', async ({ page }) => {
  await register(page, 'mobile')

  const list = page.getByRole('navigation', { name: 'Conversations' })
  const openList = page.getByRole('button', { name: 'Open conversation list' })
  await expect(openList).toBeVisible()
  await expect(list).not.toBeInViewport()

  await openList.click()
  await expect(list).toBeInViewport()
  await list.getByRole('button', { name: 'New chat' }).click()
  await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)
  // Navigating closes the panel.
  await expect(list).not.toBeInViewport()

  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeInViewport()
  await ask(page, 'hello')
  await expect(page.getByText(/Ask me anything/)).toBeVisible()

  // Nothing overflows sideways.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  )
  expect(overflow).toBeLessThanOrEqual(0)
})
