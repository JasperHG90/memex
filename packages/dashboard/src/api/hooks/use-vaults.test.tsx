import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useVaults, useDefaultVaults, useActiveVault, useCreateVault, useDeleteVault } from './use-vaults'

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

describe('useVaults', () => {
  it('fetches all vaults', async () => {
    const mockVaults = [{ id: 'v1', name: 'Default' }]
    fetchSpy.mockResolvedValue(jsonResponse(mockVaults))

    const { result } = renderHook(() => useVaults(), { wrapper: createWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockVaults)
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/vaults'),
      expect.anything(),
    )
  })
})

describe('useDefaultVaults', () => {
  it('fetches default vaults', async () => {
    const mockDefaults = [{ id: 'v1', name: 'Default' }]
    fetchSpy.mockResolvedValue(jsonResponse(mockDefaults))

    const { result } = renderHook(() => useDefaultVaults(), { wrapper: createWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(mockDefaults)
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('is_default=true'),
      expect.anything(),
    )
  })
})

describe('useActiveVault', () => {
  it('selects first active vault from response', async () => {
    const mockActive = [{ id: 'v1', name: 'Active' }, { id: 'v2', name: 'Other' }]
    fetchSpy.mockResolvedValue(jsonResponse(mockActive))

    const { result } = renderHook(() => useActiveVault(), { wrapper: createWrapper() })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual({ id: 'v1', name: 'Active' })
  })
})

describe('useCreateVault', () => {
  it('sends POST to create vault', async () => {
    const newVault = { id: 'v2', name: 'New Vault' }
    fetchSpy.mockResolvedValue(jsonResponse(newVault))

    const { result } = renderHook(() => useCreateVault(), { wrapper: createWrapper() })

    result.current.mutate({ name: 'New Vault' } as never)

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toEqual(newVault)
  })
})

describe('useDeleteVault', () => {
  it('sends DELETE request for vault', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ status: 'deleted' }))

    const { result } = renderHook(() => useDeleteVault(), { wrapper: createWrapper() })

    result.current.mutate('v1')

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/vaults/v1'),
      expect.objectContaining({ method: 'DELETE' }),
    )
  })
})
