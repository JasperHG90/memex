import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SummaryCard } from '@/components/shared/summary-card'

describe('SummaryCard', () => {
  it('renders loading skeleton when isLoading is true', () => {
    render(<SummaryCard summary={undefined} isLoading={true} />)
    expect(screen.getByText('Generating summary...')).toBeInTheDocument()
  })

  it('renders nothing when not loading and no summary', () => {
    const { container } = render(<SummaryCard summary={undefined} isLoading={false} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders summary text', () => {
    render(<SummaryCard summary="This is a test summary." isLoading={false} />)
    expect(screen.getByText('AI Summary')).toBeInTheDocument()
    expect(screen.getByText('This is a test summary.')).toBeInTheDocument()
  })

  it('renders clickable citation markers', async () => {
    const onCitationClick = vi.fn()
    const user = userEvent.setup()

    render(
      <SummaryCard
        summary="See reference [1] and [2] for details."
        isLoading={false}
        onCitationClick={onCitationClick}
      />,
    )

    const citation1 = screen.getByRole('button', { name: '[1]' })
    expect(citation1).toBeInTheDocument()

    await user.click(citation1)
    expect(onCitationClick).toHaveBeenCalledWith(1)

    const citation2 = screen.getByRole('button', { name: '[2]' })
    await user.click(citation2)
    expect(onCitationClick).toHaveBeenCalledWith(2)
  })

  it('renders summary without citations when none present', () => {
    render(<SummaryCard summary="No citations here." isLoading={false} />)
    expect(screen.getByText('No citations here.')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
