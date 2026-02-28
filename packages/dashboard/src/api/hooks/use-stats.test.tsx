import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useSystemStats, useTokenUsage, useMetrics } from './use-stats'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

let fetchSpy: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchSpy = vi.fn()
  vi.stubGlobal('fetch', fetchSpy)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('useSystemStats', () => {
  it('fetches stats counts', async () => {
    const mockStats = { notes: 10, entities: 5, memories: 20 }
    fetchSpy.mockResolvedValue(jsonResponse(mockStats))

    const { result } = renderHook(() => useSystemStats(), { wrapper: createWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockStats)
  })

  it('includes vault_id params when provided', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({}))

    renderHook(() => useSystemStats(['v1', 'v2']), { wrapper: createWrapper() })

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const url = fetchSpy.mock.calls[0][0] as string
    expect(url).toContain('vault_id=v1')
    expect(url).toContain('vault_id=v2')
  })
})

describe('useTokenUsage', () => {
  it('fetches token usage stats', async () => {
    const mockUsage = { total_tokens: 1000, prompt_tokens: 600 }
    fetchSpy.mockResolvedValue(jsonResponse(mockUsage))

    const { result } = renderHook(() => useTokenUsage(), { wrapper: createWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockUsage)
  })
})

describe('useMetrics', () => {
  it('fetches raw metrics text', async () => {
    const metricsText = '# HELP memex_notes_total\nmemex_notes_total 42'
    fetchSpy.mockResolvedValue(new Response(metricsText, { status: 200 }))

    const { result } = renderHook(() => useMetrics(), { wrapper: createWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toContain('memex_notes_total 42')
  })
})
