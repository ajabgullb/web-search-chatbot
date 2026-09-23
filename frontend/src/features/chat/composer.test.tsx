/**
 * Composer keyboard and limit behaviour.
 *
 * These are the details users notice immediately when they are wrong: a
 * newline that sends the message, or a send that fires on an empty box.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Composer } from '@/features/chat/Composer'
import { MAX_MESSAGE_CHARACTERS } from '@/features/chat/constants'

describe('the composer', () => {
  it('sends on Enter', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    render(<Composer onSubmit={onSubmit} />)

    await user.type(screen.getByLabelText(/^message$/i), 'what is a keyset cursor?{Enter}')

    expect(onSubmit).toHaveBeenCalledExactlyOnceWith('what is a keyset cursor?')
  })

  it('inserts a newline on Shift+Enter instead of sending', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    render(<Composer onSubmit={onSubmit} />)

    const textarea = screen.getByLabelText(/^message$/i)
    await user.type(textarea, 'first{Shift>}{Enter}{/Shift}second')

    expect(onSubmit).not.toHaveBeenCalled()
    expect(textarea).toHaveValue('first\nsecond')
  })

  it('clears the box after sending', async () => {
    const user = userEvent.setup()
    render(<Composer onSubmit={vi.fn()} />)

    const textarea = screen.getByLabelText(/^message$/i)
    await user.type(textarea, 'hello{Enter}')

    expect(textarea).toHaveValue('')
  })

  it('refuses to send whitespace', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    render(<Composer onSubmit={onSubmit} />)

    await user.type(screen.getByLabelText(/^message$/i), '   {Enter}')

    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /send message/i })).toBeDisabled()
  })

  it('trims before submitting', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    render(<Composer onSubmit={onSubmit} />)

    await user.type(screen.getByLabelText(/^message$/i), '  padded  {Enter}')

    expect(onSubmit).toHaveBeenCalledExactlyOnceWith('padded')
  })

  it('blocks sending past the character limit and says so', async () => {
    const onSubmit = vi.fn()
    render(<Composer onSubmit={onSubmit} />)

    const textarea = screen.getByLabelText(/^message$/i)
    // Typing 16k characters would take minutes; paste is the realistic path.
    await userEvent.setup().click(textarea)
    await userEvent.setup().paste('x'.repeat(MAX_MESSAGE_CHARACTERS + 1))

    expect(screen.getByRole('button', { name: /send message/i })).toBeDisabled()
    expect(textarea).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByText(/16,001 \/ 16,000/)).toBeInTheDocument()
  })

  it('lets the next message be drafted but not sent while a run is active', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    render(<Composer onSubmit={onSubmit} running onStop={vi.fn()} />)

    const textarea = screen.getByLabelText(/^message$/i)
    expect(textarea).toBeEnabled()
    await user.type(textarea, 'next question{Enter}')

    expect(onSubmit).not.toHaveBeenCalled()
    // Enter did not send, and did not eat the draft either.
    expect(textarea).toHaveValue('next question')
    expect(screen.queryByRole('button', { name: /send message/i })).not.toBeInTheDocument()
  })

  it('offers a stop button while a run is active', async () => {
    const user = userEvent.setup()
    const onStop = vi.fn()
    render(<Composer onSubmit={vi.fn()} running onStop={onStop} />)

    await user.click(screen.getByRole('button', { name: /stop answering/i }))

    expect(onStop).toHaveBeenCalledOnce()
  })

  it('disables stop once stopping has been requested', () => {
    render(<Composer onSubmit={vi.fn()} running stopping onStop={vi.fn()} />)

    expect(screen.getByRole('button', { name: /stopping/i })).toBeDisabled()
  })
})
