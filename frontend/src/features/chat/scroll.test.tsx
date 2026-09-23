/**
 * Auto-scroll follows the answer only while the reader is at the bottom.
 *
 * jsdom does no layout, so the element's scroll metrics are driven by hand:
 * the test plays both the growing content and the reader's scrolling.
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { MessageList } from '@/features/chat/MessageList'

function mountThread() {
  const metrics = { scrollHeight: 1000, clientHeight: 500, scrollTop: 0 }
  const { rerender } = render(
    <MessageList messages={[]} hasOlder={false} isLoadingOlder={false} onLoadOlder={() => {}} />,
  )
  const thread = screen.getByTestId('thread')
  Object.defineProperty(thread, 'scrollHeight', { get: () => metrics.scrollHeight })
  Object.defineProperty(thread, 'clientHeight', { get: () => metrics.clientHeight })
  Object.defineProperty(thread, 'scrollTop', {
    get: () => metrics.scrollTop,
    set: (value: number) => {
      metrics.scrollTop = value
    },
  })

  const grow = (height: number, key: number) => {
    metrics.scrollHeight = height
    rerender(
      <MessageList
        messages={[]}
        hasOlder={false}
        isLoadingOlder={false}
        onLoadOlder={() => {}}
        followKey={key}
      />,
    )
  }
  const readerScrollsTo = (top: number) => {
    metrics.scrollTop = top
    fireEvent.scroll(thread)
  }
  return { metrics, grow, readerScrollsTo }
}

describe('auto-scroll', () => {
  it('follows a growing answer while the reader is at the bottom', () => {
    const { metrics, grow } = mountThread()

    grow(1000, 1)
    expect(metrics.scrollTop).toBe(1000)
    grow(1400, 2)
    expect(metrics.scrollTop).toBe(1400)
  })

  it('never pulls a reader who scrolled up back down', () => {
    const { metrics, grow, readerScrollsTo } = mountThread()
    grow(1000, 1)

    readerScrollsTo(100) // 400px above the bottom: reading earlier text
    grow(1600, 2)
    grow(2400, 3)

    expect(metrics.scrollTop).toBe(100)
  })

  it('resumes following once the reader returns to the bottom', () => {
    const { metrics, grow, readerScrollsTo } = mountThread()
    grow(1000, 1)
    readerScrollsTo(100)
    grow(1600, 2)

    readerScrollsTo(1050) // within the threshold of the bottom again
    grow(2000, 3)

    expect(metrics.scrollTop).toBe(2000)
  })

  it('treats a large jump in content as growth, not as the reader leaving', () => {
    // The flaw this design avoids: measuring *after* render would see a big
    // chunk of new text as "far from the bottom" and stop following.
    const { metrics, grow } = mountThread()
    grow(1000, 1)
    grow(9000, 2)
    expect(metrics.scrollTop).toBe(9000)
  })
})
