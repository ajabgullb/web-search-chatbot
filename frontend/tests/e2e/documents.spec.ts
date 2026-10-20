import { expect, test, type Page } from '@playwright/test'

import { ask, register, startConversation } from './helpers.ts'
import { makePdf } from './pdf.ts'

/**
 * PDFs attached to a conversation (ADR-0015), end to end: the real parser and
 * ingestion, the real retrieval SQL, a scripted model and fake embeddings.
 */

const REPORT = makePdf([
  'Annual report 2025\nContents',
  'Quarterly revenue grew twelve percent on strong demand.',
])
const QUESTION = 'What does the report say about quarterly revenue?'
// The answer's own opening: the source card quotes the passage, which shares the rest.
const ANSWER_TEXT = 'The report says quarterly revenue grew twelve percent'

/** Attach a file the way a user does: the paperclip, then the file chooser. */
async function attach(page: Page, name: string, buffer: Buffer, mimeType = 'application/pdf') {
  const chooser = page.waitForEvent('filechooser')
  await page.getByRole('button', { name: 'Attach a PDF' }).click()
  await (await chooser).setFiles({ name, mimeType, buffer })
}

function attached(page: Page) {
  return page.getByRole('list', { name: 'Attached documents' })
}

test('a PDF attached in the message box is read and cited by page, until it is removed', async ({
  page,
}) => {
  await register(page, 'docs')
  await startConversation(page)

  // The button lives in the message box, beside where one types.
  const box = page.getByRole('textbox', { name: 'Message', exact: true })
  const paperclip = page.getByRole('button', { name: 'Attach a PDF' })
  await expect(paperclip).toBeVisible()
  expect(
    await paperclip.evaluate(
      (button, textarea) => button.parentElement === textarea?.parentElement,
      await box.elementHandle(),
    ),
  ).toBe(true)

  await attach(page, 'report.pdf', REPORT)
  const chip = attached(page).getByRole('listitem').filter({ hasText: 'report.pdf' })
  await expect(chip).toBeVisible()
  // Prepared in the background; the chip polls until it is ready.
  await expect(chip).toContainText('ready', { timeout: 20_000 })

  await ask(page, QUESTION)
  await expect(page.getByText(ANSWER_TEXT)).toBeVisible()
  const sources = page.getByRole('region', { name: 'Sources' })
  const card = sources.getByRole('listitem').filter({ hasText: 'report.pdf' })
  await expect(card).toContainText('Page 2 · your document')
  // A passage of the user's own file: nothing to link to.
  await expect(sources.getByRole('link')).toHaveCount(0)

  // The inline marker jumps to the card.
  await page.getByRole('button', { name: 'Source 1: report.pdf' }).click()
  await expect(card).toBeFocused()

  // A reload keeps both the document and the cited answer.
  await page.reload()
  await expect(
    attached(page).getByRole('listitem').filter({ hasText: 'report.pdf' }),
  ).toContainText('ready')
  await expect(page.getByRole('button', { name: 'Source 1: report.pdf' })).toBeVisible()

  // Removing it takes its citations with it, at once; the answer text stays.
  await page.getByRole('button', { name: 'Remove report.pdf' }).click()
  await expect(attached(page)).toBeHidden()
  await expect(page.getByRole('region', { name: 'Sources' })).toHaveCount(0)
  await expect(page.getByText(ANSWER_TEXT)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Source 1: report.pdf' })).toHaveCount(0)
})

test('a file that only claims to be a PDF is refused with the server’s reason', async ({
  page,
}) => {
  await register(page, 'docs-bad')
  await startConversation(page)

  // Right name, right type, wrong bytes: only the server can tell.
  await attach(page, 'fake.pdf', Buffer.from('just some text, not a PDF'))

  await expect(page.getByRole('alert')).toContainText(
    'Could not attach fake.pdf: The file is not a readable PDF.',
  )
  await expect(attached(page)).toBeHidden()
})

test('a document is visible only in its own conversation', async ({ page }) => {
  await register(page, 'docs-scope')
  await startConversation(page)
  await attach(page, 'report.pdf', REPORT)
  await expect(attached(page).getByRole('listitem')).toContainText('ready', { timeout: 20_000 })

  await page.goto('/chat')
  await startConversation(page)
  await expect(page.getByRole('button', { name: 'Attach a PDF' })).toBeVisible()
  await expect(attached(page)).toBeHidden()
})
