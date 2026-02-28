import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useConnectionStatus } from './use-connection-status'

describe('useConnectionStatus', () => {
  let fetchSpy: ReturnType<typeof vi.fn>
  const INTERVAL = 15_000

  beforeEach(() => {
    fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('starts with isConnected true', () => {
    fetchSpy.mockResolvedValue(new Response(null, { status: 200 }))
    const { result } = renderHook(() => useConnectionStatus())
    expect(result.current.isConnected).toBe(true)
  })

  it('sets isConnected to false when health check fails', async () => {
    fetchSpy.mockRejectedValue(new Error('Network error'))
    const { result } = renderHook(() => useConnectionStatus())

    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERVAL)
    })

    expect(result.current.isConnected).toBe(false)
  })

  it('sets isConnected to false when response is not ok', async () => {
    fetchSpy.mockResolvedValue(new Response(null, { status: 503 }))
    const { result } = renderHook(() => useConnectionStatus())

    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERVAL)
    })

    expect(result.current.isConnected).toBe(false)
  })

  it('recovers when health check succeeds after failure', async () => {
    fetchSpy.mockRejectedValueOnce(new Error('offline'))
    const { result } = renderHook(() => useConnectionStatus())

    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERVAL)
    })

    expect(result.current.isConnected).toBe(false)

    // Next call succeeds
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 200 }))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERVAL)
    })

    expect(result.current.isConnected).toBe(true)
  })

  it('clears interval on unmount', async () => {
    fetchSpy.mockResolvedValue(new Response(null, { status: 200 }))
    const { unmount } = renderHook(() => useConnectionStatus())

    unmount()

    const callCountAfterUnmount = fetchSpy.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERVAL * 2)
    })
    expect(fetchSpy.mock.calls.length).toBe(callCountAfterUnmount)
  })

  it('polls the correct endpoint', async () => {
    fetchSpy.mockResolvedValue(new Response(null, { status: 200 }))
    renderHook(() => useConnectionStatus())

    await act(async () => {
      await vi.advanceTimersByTimeAsync(INTERVAL)
    })

    expect(fetchSpy).toHaveBeenCalledWith('/api/v1/stats/counts', { method: 'GET' })
  })
})
