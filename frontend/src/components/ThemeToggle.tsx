import { Moon, Sun } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { useTheme } from '@/hooks/useTheme'

export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme()
  const isDark = document.documentElement.classList.contains('dark')

  return (
    <Button
      variant="ghost"
      size="icon"
      type="button"
      className={className}
      onClick={() => setTheme(isDark ? 'light' : 'dark')}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      title={`Theme: ${theme}`}
    >
      {isDark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </Button>
  )
}
