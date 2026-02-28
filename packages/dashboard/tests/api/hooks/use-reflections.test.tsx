import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useReflectionQueue, useTriggerReflection } from '@/api/hooks/use-reflections'

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
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

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('useReflectionQueue', () => {
  it('fetches reflection queue', async () => {
    const mockQueue = [{ id: 'r1', entity_id: 'e1', status: 'queued' }]
    fetchSpy.mockResolvedValue(jsonResponse(mockQueue))

    const { result } = renderHook(
      () => useReflectionQueue(),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockQueue)
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('status=queued'),
      expect.anything(),
    )
  })

  it('includes vault_id params when provided', async () => {
    fetchSpy.mockResolvedValue(jsonResponse([]))

    renderHook(
      () => useReflectionQueue(['v1', 'v2']),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const url = fetchSpy.mock.calls[0][0] as string
    expect(url).toContain('vault_id=v1')
    expect(url).toContain('vault_id=v2')
  })
})

describe('useTriggerReflection', () => {
  it('sends POST to trigger reflection', async () => {
    const mockResult = { id: 'r1', status: 'completed' }
    fetchSpy.mockResolvedValue(jsonResponse(mockResult))

    const { result } = renderHook(
      () => useTriggerReflection(),
      { wrapper: createWrapper() },
    )

    result.current.mutate('e1')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockResult)
    const [, options] = fetchSpy.mock.calls[0]
    expect(options.method).toBe('POST')
    expect(JSON.parse(options.body)).toEqual({ entity_id: 'e1' })
  })
})
