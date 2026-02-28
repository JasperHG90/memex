import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ErrorState } from '@/components/shared/error-state'

describe('ErrorState', () => {
  it('renders default title and provided message', () => {
    render(<ErrorState message="Connection refused" />)

    expect(screen.getByText('Something went wrong')).toBeInTheDocument()
    expect(screen.getByText('Connection refused')).toBeInTheDocument()
  })

  it('renders custom title when provided', () => {
    render(<ErrorState title="Load Failed" message="Could not fetch data" />)

    expect(screen.getByText('Load Failed')).toBeInTheDocument()
    expect(screen.getByText('Could not fetch data')).toBeInTheDocument()
  })

  it('renders retry button when onRetry is provided', () => {
    render(<ErrorState message="Error" onRetry={() => {}} />)

    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.getByText('Try Again')).toBeInTheDocument()
  })

  it('does not render retry button when onRetry is not provided', () => {
    render(<ErrorState message="Error" />)

    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('calls onRetry when retry button is clicked', async () => {
    const onRetry = vi.fn()
    const user = userEvent.setup()

    render(<ErrorState message="Error" onRetry={onRetry} />)

    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalledOnce()
  })
})
