import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Search } from 'lucide-react'
import { EmptyState } from '@/components/shared/empty-state'

describe('EmptyState', () => {
  it('renders title and description', () => {
    render(
      <EmptyState
        icon={Search}
        title="No results found"
        description="Try a different search query."
      />
    )

    expect(screen.getByText('No results found')).toBeInTheDocument()
    expect(screen.getByText('Try a different search query.')).toBeInTheDocument()
  })

  it('renders action button when provided', async () => {
    const onClick = vi.fn()
    const user = userEvent.setup()

    render(
      <EmptyState
        icon={Search}
        title="No results"
        description="Nothing here."
        action={{ label: 'Retry', onClick }}
      />
    )

    const button = screen.getByRole('button', { name: 'Retry' })
    expect(button).toBeInTheDocument()

    await user.click(button)
    expect(onClick).toHaveBeenCalledOnce()
  })

  it('renders suggestion chips when provided', async () => {
    const onSuggestionClick = vi.fn()
    const user = userEvent.setup()

    render(
      <EmptyState
        icon={Search}
        title="No results"
        description="Try one of these:"
        suggestions={[
          { label: 'Option A', onClick: onSuggestionClick },
          { label: 'Option B', onClick: onSuggestionClick },
        ]}
      />
    )

    expect(screen.getByText('Option A')).toBeInTheDocument()
    expect(screen.getByText('Option B')).toBeInTheDocument()

    await user.click(screen.getByText('Option A'))
    expect(onSuggestionClick).toHaveBeenCalledOnce()
  })

  it('does not render action button when not provided', () => {
    render(
      <EmptyState
        icon={Search}
        title="No results"
        description="Nothing here."
      />
    )

    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
