import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useKeyboardShortcuts } from '@/hooks/use-keyboard-shortcuts'

function fireKeyDown(key: string, opts: Partial<KeyboardEventInit> = {}) {
  document.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, ...opts }))
}

describe('useKeyboardShortcuts', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('calls onCommandPalette on Ctrl+K', () => {
    const onCommandPalette = vi.fn()
    renderHook(() => useKeyboardShortcuts({ onCommandPalette }))

    fireKeyDown('k', { ctrlKey: true })
    expect(onCommandPalette).toHaveBeenCalledOnce()
  })

  it('calls onCommandPalette on Meta+K (macOS)', () => {
    const onCommandPalette = vi.fn()
    renderHook(() => useKeyboardShortcuts({ onCommandPalette }))

    fireKeyDown('k', { metaKey: true })
    expect(onCommandPalette).toHaveBeenCalledOnce()
  })

  it('calls onQuickNote on Ctrl+N', () => {
    const onQuickNote = vi.fn()
    renderHook(() => useKeyboardShortcuts({ onQuickNote }))

    fireKeyDown('n', { ctrlKey: true })
    expect(onQuickNote).toHaveBeenCalledOnce()
  })

  it('calls onEscape on Escape key', () => {
    const onEscape = vi.fn()
    renderHook(() => useKeyboardShortcuts({ onEscape }))

    fireKeyDown('Escape')
    expect(onEscape).toHaveBeenCalledOnce()
  })

  it('does not call handlers for unrelated keys', () => {
    const onCommandPalette = vi.fn()
    const onQuickNote = vi.fn()
    const onEscape = vi.fn()
    renderHook(() => useKeyboardShortcuts({ onCommandPalette, onQuickNote, onEscape }))

    fireKeyDown('a')
    fireKeyDown('Enter')
    expect(onCommandPalette).not.toHaveBeenCalled()
    expect(onQuickNote).not.toHaveBeenCalled()
    expect(onEscape).not.toHaveBeenCalled()
  })

  it('skips non-Escape shortcuts when target is an INPUT', () => {
    const onCommandPalette = vi.fn()
    const onEscape = vi.fn()
    renderHook(() => useKeyboardShortcuts({ onCommandPalette, onEscape }))

    const input = document.createElement('input')
    document.body.appendChild(input)
    input.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'k', ctrlKey: true, bubbles: true,
    }))
    expect(onCommandPalette).not.toHaveBeenCalled()

    // Escape should still work in inputs
    input.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape', bubbles: true,
    }))
    expect(onEscape).toHaveBeenCalledOnce()

    document.body.removeChild(input)
  })

  it('skips non-Escape shortcuts when target is a TEXTAREA', () => {
    const onQuickNote = vi.fn()
    renderHook(() => useKeyboardShortcuts({ onQuickNote }))

    const textarea = document.createElement('textarea')
    document.body.appendChild(textarea)
    textarea.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'n', ctrlKey: true, bubbles: true,
    }))
    expect(onQuickNote).not.toHaveBeenCalled()

    document.body.removeChild(textarea)
  })

  it('cleans up event listener on unmount', () => {
    const onCommandPalette = vi.fn()
    const { unmount } = renderHook(() => useKeyboardShortcuts({ onCommandPalette }))

    unmount()

    fireKeyDown('k', { ctrlKey: true })
    expect(onCommandPalette).not.toHaveBeenCalled()
  })

  it('does not register duplicate listeners', () => {
    const onCommandPalette = vi.fn()
    const { rerender } = renderHook(() => useKeyboardShortcuts({ onCommandPalette }))

    // Re-render the hook
    rerender()

    fireKeyDown('k', { ctrlKey: true })
    // Should only be called once, not twice
    expect(onCommandPalette).toHaveBeenCalledOnce()
  })
})
