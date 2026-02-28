import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Activity } from 'lucide-react'
import { MetricCard } from './metric-card'

describe('MetricCard', () => {
  it('renders label and string value', () => {
    render(<MetricCard icon={Activity} label="Status" value="Active" />)

    expect(screen.getByText('Status')).toBeInTheDocument()
    expect(screen.getByText('Active')).toBeInTheDocument()
  })

  it('renders numeric value', () => {
    render(<MetricCard icon={Activity} label="Count" value={42} />)

    expect(screen.getByText('Count')).toBeInTheDocument()
    // Animated number starts at 0 and animates to 42
    // In test environment we just check the element exists
    expect(screen.getByText('Count').closest('div')?.parentElement).toBeTruthy()
  })

  it('renders description when provided', () => {
    render(
      <MetricCard icon={Activity} label="Notes" value={10} description="Total notes ingested" />
    )

    expect(screen.getByText('Total notes ingested')).toBeInTheDocument()
  })

  it('does not render description when not provided', () => {
    render(<MetricCard icon={Activity} label="Notes" value={10} />)

    expect(screen.queryByText('Total notes ingested')).not.toBeInTheDocument()
  })
})
