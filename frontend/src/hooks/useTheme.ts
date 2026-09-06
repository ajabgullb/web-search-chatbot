import { useCallback, useEffect, useState } from 'react'

export type Theme = 'light' | 'dark' | 'system'

const STORAGE_KEY = 'theme'

function readStored(): Theme {
  try {
    // eslint-disable-next-line no-restricted-properties -- display preference, not a credential
    const value = window.localStorage.getItem(STORAGE_KEY)
    return value === 'light' || value === 'dark' ? value : 'system'
  } catch {
    // Private browsing or blocked site data: fall back to the OS preference.
    return 'system'
  }
}

function apply(theme: Theme): void {
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
  const dark = theme === 'dark' || (theme === 'system' && prefersDark)
  document.documentElement.classList.toggle('dark', dark)
}

/**
 * Light/dark theme with an explicit override on top of the OS preference.
 *
 * This is the one thing the app does keep in `localStorage`. It is a display
 * preference, not a credential — the ESLint ban covers `localStorage` as a
 * global, so this module reaches it through `window` deliberately.
 */
export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(readStored)

  useEffect(() => {
    apply(theme)
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => {
      if (theme === 'system') apply('system')
    }
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [theme])

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next)
    try {
      // eslint-disable-next-line no-restricted-properties -- display preference, not a credential
      window.localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // Not being able to remember the choice is not worth failing over.
    }
  }, [])

  return { theme, setTheme }
}
