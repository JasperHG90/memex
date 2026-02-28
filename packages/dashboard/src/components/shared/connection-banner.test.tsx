import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ConnectionBanner } from './connection-banner'

describe('ConnectionBanner', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows error message when isError is true', () => {
    render(<ConnectionBanner isError={true} />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/Unable to connect/)).toBeInTheDocument()
  })

  it('does not render when isError is false and no previous error', () => {
    render(<ConnectionBanner isError={false} />)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows restored message when error clears', () => {
    const { rerender } = render(<ConnectionBanner isError={true} />)
    expect(screen.getByText(/Unable to connect/)).toBeInTheDocument()

    rerender(<ConnectionBanner isError={false} />)
    expect(screen.getByText(/Connection restored/)).toBeInTheDocument()
  })

  it('auto-hides restored message after delay', async () => {
    const { rerender } = render(<ConnectionBanner isError={true} />)
    rerender(<ConnectionBanner isError={false} />)

    expect(screen.getByText(/Connection restored/)).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600)
    })

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('can be dismissed via close button', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    render(<ConnectionBanner isError={true} />)

    expect(screen.getByRole('alert')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /dismiss/i }))

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
