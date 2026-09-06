/**
 * Markdown rendering, with the emphasis on what must *not* happen.
 *
 * Assistant content is model output, and from Phase 5 it will paraphrase pages
 * fetched from the open web. It is treated as hostile input throughout.
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import Markdown from '@/features/chat/Markdown'

describe('markdown rendering', () => {
  it('renders ordinary formatting', () => {
    const { container } = render(<Markdown content={'# Title\n\nSome **bold** text.'} />)

    expect(screen.getByRole('heading', { name: 'Title' })).toBeInTheDocument()
    expect(container.querySelector('strong')).toHaveTextContent('bold')
  })

  it('renders fenced code with a language class for highlighting', () => {
    const { container } = render(<Markdown content={'```ts\nconst x: number = 1\n```'} />)

    const code = container.querySelector('pre code')
    expect(code).toBeInTheDocument()
    expect(code?.className).toContain('language-ts')
    // rehype-highlight ran and tokenised it.
    expect(container.querySelector('.hljs-keyword')).toBeInTheDocument()
  })

  it('does not execute or emit a script tag', () => {
    const { container } = render(
      <Markdown content={'Hello <script>window.pwned = true</script> world'} />,
    )

    expect(container.querySelector('script')).toBeNull()
    expect('pwned' in window).toBe(false)
  })

  it('does not emit an img with an event handler', () => {
    const { container } = render(<Markdown content={'<img src=x onerror="alert(1)">'} />)

    expect(container.querySelector('img')).toBeNull()
    expect(container.innerHTML).not.toContain('onerror')
  })

  it('strips a javascript: link but keeps its text', () => {
    const { container } = render(<Markdown content={'[click me](javascript:alert(1))'} />)

    expect(container.querySelector('a')).toBeNull()
    expect(screen.getByText('click me')).toBeInTheDocument()
  })

  it('opens http(s) links safely in a new tab', () => {
    render(<Markdown content={'[a source](https://example.com/article)'} />)

    const link = screen.getByRole('link', { name: 'a source' })
    expect(link).toHaveAttribute('href', 'https://example.com/article')
    // noopener stops the opened page reaching back through window.opener.
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('rejects other schemes such as data: and file:', () => {
    const { container } = render(
      <Markdown content={'[a](data:text/html,<h1>x</h1>) and [b](file:///etc/passwd)'} />,
    )

    expect(container.querySelectorAll('a')).toHaveLength(0)
  })
})
