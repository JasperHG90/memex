import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useNotes, useNote, useNoteSearch, useIngestNote, useDeleteNote } from './use-notes'

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

describe('useNotes', () => {
  it('fetches notes list', async () => {
    const mockNotes = [{ id: 'n1', title: 'Note 1' }]
    fetchSpy.mockResolvedValue(jsonResponse(mockNotes))

    const { result } = renderHook(() => useNotes(), { wrapper: createWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockNotes)
  })

  it('includes query params when options provided', async () => {
    fetchSpy.mockResolvedValue(jsonResponse([]))

    renderHook(
      () => useNotes({ limit: 5, offset: 10, sort: '-created_at', vaultIds: ['v1'] }),
      { wrapper: createWrapper() },
    )

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const url = fetchSpy.mock.calls[0][0] as string
    expect(url).toContain('limit=5')
    expect(url).toContain('offset=10')
    expect(url).toContain('sort=-created_at')
    expect(url).toContain('vault_id=v1')
  })
})

describe('useNote', () => {
  it('fetches a single note by id', async () => {
    const mockNote = { id: 'n1', title: 'Test Note' }
    fetchSpy.mockResolvedValue(jsonResponse(mockNote))

    const { result } = renderHook(() => useNote('n1'), { wrapper: createWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockNote)
  })

  it('is disabled when noteId is undefined', () => {
    const { result } = renderHook(() => useNote(undefined), { wrapper: createWrapper() })

    expect(result.current.fetchStatus).toBe('idle')
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})

describe('useNoteSearch', () => {
  it('sends POST to search notes', async () => {
    const mockResults = [{ note_id: 'n1', score: 0.9 }]
    fetchSpy.mockResolvedValue(jsonResponse(mockResults))

    const { result } = renderHook(() => useNoteSearch(), { wrapper: createWrapper() })

    result.current.mutate({ query: 'test' } as never)

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockResults)
  })
})

describe('useIngestNote', () => {
  it('sends POST to ingest a new note', async () => {
    const mockResponse = { note_id: 'n2', status: 'ingested' }
    fetchSpy.mockResolvedValue(jsonResponse(mockResponse))

    const { result } = renderHook(() => useIngestNote(), { wrapper: createWrapper() })

    result.current.mutate({ title: 'New Note', markdown_content: '# Hello' } as never)

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockResponse)
  })
})

describe('useDeleteNote', () => {
  it('sends DELETE request for note', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ status: 'deleted' }))

    const { result } = renderHook(() => useDeleteNote(), { wrapper: createWrapper() })

    result.current.mutate('n1')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/notes/n1'),
      expect.objectContaining({ method: 'DELETE' }),
    )
  })
})
