import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ErrorBoundary } from './error-boundary'

function ThrowingComponent({ error }: { error?: Error }) {
  if (error) {
    throw error
  }
  return <div>Content rendered successfully</div>
}

describe('ErrorBoundary', () => {
  beforeEach(() => {
    // Suppress React error boundary console.error noise in test output
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  it('renders children when no error occurs', () => {
    render(
      <ErrorBoundary>
        <ThrowingComponent />
      </ErrorBoundary>
    )

    expect(screen.getByText('Content rendered successfully')).toBeInTheDocument()
  })

  it('renders error UI when child throws', () => {
    render(
      <ErrorBoundary>
        <ThrowingComponent error={new Error('Test render failure')} />
      </ErrorBoundary>
    )

    expect(screen.getByText('Something went wrong')).toBeInTheDocument()
    expect(screen.getByText('Test render failure')).toBeInTheDocument()
  })

  it('renders retry button that resets error state', async () => {
    const user = userEvent.setup()

    const { rerender } = render(
      <ErrorBoundary>
        <ThrowingComponent error={new Error('Boom')} />
      </ErrorBoundary>
    )

    expect(screen.getByText('Something went wrong')).toBeInTheDocument()

    // After clicking retry, the boundary resets. Re-render without error.
    rerender(
      <ErrorBoundary>
        <ThrowingComponent />
      </ErrorBoundary>
    )

    await user.click(screen.getByRole('button', { name: /retry/i }))

    expect(screen.getByText('Content rendered successfully')).toBeInTheDocument()
  })

  it('renders custom fallback when provided', () => {
    render(
      <ErrorBoundary fallback={<div>Custom error page</div>}>
        <ThrowingComponent error={new Error('Oops')} />
      </ErrorBoundary>
    )

    expect(screen.getByText('Custom error page')).toBeInTheDocument()
  })
})
