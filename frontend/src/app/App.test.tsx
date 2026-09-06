import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { App } from '@/app/App'

describe('App shell', () => {
  it('renders the landing page at the root route', () => {
    window.history.pushState({}, '', '/')
    render(<App />)

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/answers you can check/i)
    expect(screen.getByRole('link', { name: /create an account/i })).toBeInTheDocument()
  })

  it('renders the 404 page for an unknown route', () => {
    window.history.pushState({}, '', '/does-not-exist')
    render(<App />)

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/page not found/i)
  })
})
