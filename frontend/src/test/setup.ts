import '@testing-library/jest-dom/vitest'

import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// React Testing Library does not auto-clean when `globals` is enabled in some
// setups; doing it explicitly keeps tests isolated either way.
afterEach(() => {
  cleanup()
})
