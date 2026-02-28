import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import {
  useMemory,
  useMemorySearch,
  useDeleteMemory,
  useAdjustBelief,
  useMemoryLineage,
} from './use-memories'

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

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('useMemory', () => {
  it('fetches a memory unit by id', async () => {
    const mockMemory = { id: 'mem-1', content: 'Test fact' }
    fetchSpy.mockResolvedValue(jsonResponse(mockMemory))

    const { result } = renderHook(() => useMemory('mem-1'), { wrapper: createWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockMemory)
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/memories/mem-1'),
      expect.anything(),
    )
  })

  it('is disabled when unitId is undefined', () => {
    const { result } = renderHook(() => useMemory(undefined), { wrapper: createWrapper() })

    expect(result.current.fetchStatus).toBe('idle')
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})

describe('useMemorySearch', () => {
  it('sends POST to search memories', async () => {
    const mockResults = [{ id: 'mem-1', content: 'Test' }]
    fetchSpy.mockResolvedValue(jsonResponse(mockResults))

    const { result } = renderHook(() => useMemorySearch(), { wrapper: createWrapper() })

    result.current.mutate({ query: 'test query' } as never)

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockResults)
  })
})

describe('useAdjustBelief', () => {
  it('sends PATCH to confirm a memory', async () => {
    fetchSpy.mockResolvedValue(
      new Response(null, { status: 204, headers: { 'Content-Type': 'application/json' } }),
    )

    const { result } = renderHook(() => useAdjustBelief(), { wrapper: createWrapper() })

    result.current.mutate({ unitId: 'mem-1', adjustment: 'confirm' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    const [url, options] = fetchSpy.mock.calls[0]
    expect(url).toContain('/memories/mem-1/belief')
    expect(options.method).toBe('PATCH')
    const body = JSON.parse(options.body)
    expect(body.evidence_type_key).toBe('user_validation')
  })

  it('sends PATCH to contradict a memory', async () => {
    fetchSpy.mockResolvedValue(
      new Response(null, { status: 204, headers: { 'Content-Type': 'application/json' } }),
    )

    const { result } = renderHook(() => useAdjustBelief(), { wrapper: createWrapper() })

    result.current.mutate({ unitId: 'mem-1', adjustment: 'contradict' })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    const body = JSON.parse(fetchSpy.mock.calls[0][1].body)
    expect(body.evidence_type_key).toBe('user_rejection')
  })
})

describe('useDeleteMemory', () => {
  it('sends DELETE request for memory', async () => {
    fetchSpy.mockResolvedValue(
      new Response(null, { status: 204, headers: { 'Content-Type': 'application/json' } }),
    )

    const { result } = renderHook(() => useDeleteMemory(), { wrapper: createWrapper() })

    result.current.mutate('mem-1')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/memories/mem-1'),
      expect.objectContaining({ method: 'DELETE' }),
    )
  })
})

describe('useMemoryLineage', () => {
  it('fetches lineage for a memory unit', async () => {
    const mockLineage = { nodes: [], edges: [] }
    fetchSpy.mockResolvedValue(jsonResponse(mockLineage))

    const { result } = renderHook(
      () => useMemoryLineage('mem-1', { direction: 'upstream', depth: 3 }),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockLineage)
    const url = fetchSpy.mock.calls[0][0] as string
    expect(url).toContain('direction=upstream')
    expect(url).toContain('depth=3')
  })

  it('is disabled when unitId is undefined', () => {
    const { result } = renderHook(
      () => useMemoryLineage(undefined),
      { wrapper: createWrapper() },
    )
    expect(result.current.fetchStatus).toBe('idle')
  })
})
