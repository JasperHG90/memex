import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { LoadingButton } from '@/components/shared/loading-button'

describe('LoadingButton', () => {
  it('renders children text', () => {
    render(<LoadingButton>Submit</LoadingButton>)
    expect(screen.getByRole('button', { name: 'Submit' })).toBeInTheDocument()
  })

  it('is disabled when loading is true', () => {
    render(<LoadingButton loading>Submit</LoadingButton>)
    expect(screen.getByRole('button')).toBeDisabled()
  })

  it('is disabled when disabled prop is true', () => {
    render(<LoadingButton disabled>Submit</LoadingButton>)
    expect(screen.getByRole('button')).toBeDisabled()
  })

  it('is enabled when neither loading nor disabled', () => {
    render(<LoadingButton>Submit</LoadingButton>)
    expect(screen.getByRole('button')).toBeEnabled()
  })

  it('calls onClick when clicked and not loading', async () => {
    const onClick = vi.fn()
    const user = userEvent.setup()
    render(<LoadingButton onClick={onClick}>Submit</LoadingButton>)

    await user.click(screen.getByRole('button'))
    expect(onClick).toHaveBeenCalledOnce()
  })
})
