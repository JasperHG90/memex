import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useLineage, useEntitySearch } from './use-lineage'

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

describe('useLineage', () => {
  it('fetches lineage data for an entity', async () => {
    const mockLineage = { nodes: [], edges: [] }
    fetchSpy.mockResolvedValue(jsonResponse(mockLineage))

    const { result } = renderHook(
      () => useLineage('e1', 'entity', 4),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockLineage)
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/lineage/entity/e1'),
      expect.anything(),
    )
  })

  it('is disabled when id is null', () => {
    const { result } = renderHook(
      () => useLineage(null),
      { wrapper: createWrapper() },
    )
    expect(result.current.fetchStatus).toBe('idle')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('includes depth parameter in URL', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ nodes: [], edges: [] }))

    renderHook(
      () => useLineage('e1', 'note', 6),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const url = fetchSpy.mock.calls[0][0] as string
    expect(url).toContain('depth=6')
    expect(url).toContain('/lineage/note/e1')
  })
})

describe('useEntitySearch', () => {
  it('fetches entities with query filter', async () => {
    const mockEntities = [
      { id: 'e1', name: 'Alice Smith', type: 'Person' },
      { id: 'e2', name: 'Alice Jones', type: 'Person' },
    ]
    fetchSpy.mockResolvedValue(jsonResponse(mockEntities))

    const { result } = renderHook(
      () => useEntitySearch('alice'),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockEntities)
  })

  it('filters results client-side when query length >= 2', async () => {
    const mockEntities = [
      { id: 'e1', name: 'Alice Smith', type: 'Person' },
      { id: 'e2', name: 'Bob Jones', type: 'Person' },
    ]
    fetchSpy.mockResolvedValue(jsonResponse(mockEntities))

    const { result } = renderHook(
      () => useEntitySearch('alice'),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    // Should filter out Bob since query is "alice"
    expect(result.current.data).toHaveLength(1)
    expect(result.current.data![0].name).toBe('Alice Smith')
  })

  it('returns top entities by mentions when query is short', async () => {
    const mockEntities = [{ id: 'e1', name: 'Alice', type: 'Person' }]
    fetchSpy.mockResolvedValue(jsonResponse(mockEntities))

    renderHook(
      () => useEntitySearch('a'),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const url = fetchSpy.mock.calls[0][0] as string
    expect(url).toContain('sort=-mentions')
  })

  it('is disabled when enabled is false', () => {
    const { result } = renderHook(
      () => useEntitySearch('alice', false),
      { wrapper: createWrapper() },
    )
    expect(result.current.fetchStatus).toBe('idle')
  })
})
