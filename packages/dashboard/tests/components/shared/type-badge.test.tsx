import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { TypeBadge } from '@/components/shared/type-badge'

describe('TypeBadge', () => {
  it('renders formatted label for underscore-separated type', () => {
    render(<TypeBadge type="memory_unit" />)
    expect(screen.getByText('Memory Unit')).toBeInTheDocument()
  })

  it('renders formatted label for single-word type', () => {
    render(<TypeBadge type="note" />)
    expect(screen.getByText('Note')).toBeInTheDocument()
  })

  it('renders already-capitalized type', () => {
    render(<TypeBadge type="Person" />)
    expect(screen.getByText('Person')).toBeInTheDocument()
  })

  it('applies known type color class', () => {
    const { container } = render(<TypeBadge type="Person" />)
    const badge = container.querySelector('span')
    expect(badge?.className).toContain('text-blue-400')
  })

  it('applies default color for unknown type', () => {
    const { container } = render(<TypeBadge type="unknown_type" />)
    const badge = container.querySelector('span')
    expect(badge?.className).toContain('text-zinc-400')
  })

  it('applies custom className', () => {
    const { container } = render(<TypeBadge type="note" className="ml-2" />)
    const badge = container.querySelector('span')
    expect(badge?.className).toContain('ml-2')
  })
})
