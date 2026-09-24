import { expect, test } from '@playwright/test'

import { ask, register, SLOW_QUESTION, startConversation } from './helpers.ts'

test('stopping an answer stops it on the server, and it can be retried', async ({ page }) => {
  await register(page, 'stop')
  await startConversation(page)
  await ask(page, SLOW_QUESTION)

  const stop = page.getByRole('button', { name: 'Stop answering' })
  await expect(stop).toBeVisible()
  await expect(page.getByText(/Gardening tip 1:/)).toBeVisible()
  await stop.click()

  await expect(page.getByText('Stopped.', { exact: true })).toBeVisible()
  // Screen-reader users hear it too, through the live region.
  await expect(page.getByRole('status')).toHaveText('Answer stopped.')
  await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Send message' })).toBeVisible()

  // The server kept no answer: after a reload the question stands unanswered.
  await page.reload()
  await expect(page.getByText('This message did not get an answer.')).toBeVisible()

  // Retry answers the same question again, without a second copy of it.
  await page.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByRole('button', { name: 'Stop answering' })).toBeVisible()
  await expect(page.getByLabel('Your message')).toHaveCount(1)
  await page.getByRole('button', { name: 'Stop answering' }).click()
  await expect(page.getByText('Stopped.', { exact: true })).toBeVisible()
})
