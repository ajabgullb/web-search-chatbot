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

  it('accepts nothing while a run is active', () => {
    render(<Composer onSubmit={vi.fn()} busy />)

    expect(screen.getByLabelText(/^message$/i)).toBeDisabled()
    expect(screen.getByRole('button', { name: /send message/i })).toBeDisabled()
  })
})
