import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import {
  useEntities,
  useEntity,
  useEntityMentions,
  useEntityCooccurrences,
  useBulkCooccurrences,
  useEntityLineage,
} from '@/api/hooks/use-entities'

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

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('useEntities', () => {
  it('fetches entities list', async () => {
    const mockEntities = [{ id: 'e1', name: 'Alice', type: 'Person' }]
    fetchSpy.mockResolvedValue(jsonResponse(mockEntities))

    const { result } = renderHook(() => useEntities(), { wrapper: createWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockEntities)
  })

  it('includes query params when options provided', async () => {
    fetchSpy.mockResolvedValue(jsonResponse([]))

    renderHook(
      () => useEntities({ limit: 10, q: 'alice', sort: '-mentions', vaultIds: ['v1'] }),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const url = fetchSpy.mock.calls[0][0] as string
    expect(url).toContain('limit=10')
    expect(url).toContain('q=alice')
    expect(url).toContain('sort=-mentions')
    expect(url).toContain('vault_id=v1')
  })
})

describe('useEntity', () => {
  it('fetches a single entity by id', async () => {
    const mockEntity = { id: 'e1', name: 'Alice', type: 'Person' }
    fetchSpy.mockResolvedValue(jsonResponse(mockEntity))

    const { result } = renderHook(() => useEntity('e1'), { wrapper: createWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockEntity)
  })

  it('is disabled when entityId is undefined', () => {
    const { result } = renderHook(() => useEntity(undefined), { wrapper: createWrapper() })
    expect(result.current.fetchStatus).toBe('idle')
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})

describe('useEntityMentions', () => {
  it('fetches entity mentions', async () => {
    const mockMentions = [{ unit_id: 'u1', content: 'Alice is an engineer' }]
    fetchSpy.mockResolvedValue(jsonResponse(mockMentions))

    const { result } = renderHook(
      () => useEntityMentions('e1'),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockMentions)
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/entities/e1/mentions'),
      expect.anything(),
    )
  })

  it('is disabled when entityId is undefined', () => {
    const { result } = renderHook(
      () => useEntityMentions(undefined),
      { wrapper: createWrapper() },
    )
    expect(result.current.fetchStatus).toBe('idle')
  })
})

describe('useEntityCooccurrences', () => {
  it('fetches entity cooccurrences', async () => {
    const mockCooccurrences = [{ entity_id: 'e2', name: 'Bob', count: 5 }]
    fetchSpy.mockResolvedValue(jsonResponse(mockCooccurrences))

    const { result } = renderHook(
      () => useEntityCooccurrences('e1'),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockCooccurrences)
  })

  it('is disabled when entityId is undefined', () => {
    const { result } = renderHook(
      () => useEntityCooccurrences(undefined),
      { wrapper: createWrapper() },
    )
    expect(result.current.fetchStatus).toBe('idle')
  })
})

describe('useBulkCooccurrences', () => {
  it('fetches bulk cooccurrences', async () => {
    const mockData = [{ entity_id: 'e2', name: 'Bob', count: 3 }]
    fetchSpy.mockResolvedValue(jsonResponse(mockData))

    const { result } = renderHook(
      () => useBulkCooccurrences(['e1', 'e2']),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockData)
    const url = fetchSpy.mock.calls[0][0] as string
    expect(url).toContain('ids=e1%2Ce2')
  })

  it('includes vault_id params', async () => {
    fetchSpy.mockResolvedValue(jsonResponse([]))

    renderHook(
      () => useBulkCooccurrences(['e1'], ['v1']),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const url = fetchSpy.mock.calls[0][0] as string
    expect(url).toContain('vault_id=v1')
  })

  it('is disabled when entityIds is empty', () => {
    const { result } = renderHook(
      () => useBulkCooccurrences([]),
      { wrapper: createWrapper() },
    )
    expect(result.current.fetchStatus).toBe('idle')
  })
})

describe('useEntityLineage', () => {
  it('fetches entity lineage', async () => {
    const mockLineage = { nodes: [], edges: [] }
    fetchSpy.mockResolvedValue(jsonResponse(mockLineage))

    const { result } = renderHook(
      () => useEntityLineage('e1', { direction: 'upstream', depth: 3, limit: 5 }),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockLineage)
    const url = fetchSpy.mock.calls[0][0] as string
    expect(url).toContain('/entities/e1/lineage')
    expect(url).toContain('direction=upstream')
    expect(url).toContain('depth=3')
    expect(url).toContain('limit=5')
  })

  it('is disabled when entityId is undefined', () => {
    const { result } = renderHook(
      () => useEntityLineage(undefined),
      { wrapper: createWrapper() },
    )
    expect(result.current.fetchStatus).toBe('idle')
  })
})
