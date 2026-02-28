import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { apiFetch, ApiError, api } from '@/api/client'

// Mock import.meta.env
vi.stubEnv('VITE_API_BASE', '/api/v1')

function jsonResponse(body: unknown, status = 200, headers?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

function ndjsonResponse(items: unknown[]): Response {
  const body = items.map((i) => JSON.stringify(i)).join('\n') + '\n'
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'application/x-ndjson' },
  })
}

describe('apiFetch', () => {
  let fetchSpy: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
  })

  it('makes GET request to correct URL', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ data: 'test' }))
    const result = await apiFetch('/vaults')
    expect(fetchSpy).toHaveBeenCalledWith('/api/v1/vaults', expect.objectContaining({
      headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
    }))
    expect(result).toEqual({ data: 'test' })
  })

  it('sets Content-Type header by default', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({}))
    await apiFetch('/test')
    const [, options] = fetchSpy.mock.calls[0]
    expect(options.headers['Content-Type']).toBe('application/json')
  })

  it('allows custom headers to override defaults', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({}))
    await apiFetch('/test', { headers: { 'Content-Type': 'text/plain' } })
    const [, options] = fetchSpy.mock.calls[0]
    expect(options.headers['Content-Type']).toBe('text/plain')
  })

  it('throws ApiError on non-ok response with detail from body', async () => {
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Not found' }), {
        status: 404,
        statusText: 'Not Found',
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    await expect(apiFetch('/missing')).rejects.toThrow(ApiError)
  })

  it('extracts detail and status from error response', async () => {
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Resource not found' }), {
        status: 404,
        statusText: 'Not Found',
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    try {
      await apiFetch('/missing')
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError)
      expect((e as ApiError).status).toBe(404)
      expect((e as ApiError).detail).toBe('Resource not found')
    }
  })

  it('uses statusText when error body has no detail', async () => {
    fetchSpy.mockResolvedValue(
      new Response('not json', {
        status: 500,
        statusText: 'Internal Server Error',
      }),
    )
    try {
      await apiFetch('/fail')
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError)
      expect((e as ApiError).detail).toBe('Internal Server Error')
    }
  })

  it('returns raw response when rawResponse is true', async () => {
    const response = jsonResponse({ data: 'raw' })
    fetchSpy.mockResolvedValue(response)
    const result = await apiFetch('/test', { rawResponse: true })
    expect(result).toBeInstanceOf(Response)
  })

  it('detects NDJSON content type and collects items', async () => {
    fetchSpy.mockResolvedValue(ndjsonResponse([{ id: 1 }, { id: 2 }]))
    const result = await apiFetch('/stream')
    expect(result).toEqual([{ id: 1 }, { id: 2 }])
  })

  it('returns undefined for 204 No Content', async () => {
    fetchSpy.mockResolvedValue(
      new Response(null, {
        status: 204,
        statusText: 'No Content',
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const result = await apiFetch('/delete')
    expect(result).toBeUndefined()
  })

  it('passes through request options', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({}))
    await apiFetch('/test', { method: 'POST', body: '{"key":"value"}' })
    const [, options] = fetchSpy.mock.calls[0]
    expect(options.method).toBe('POST')
    expect(options.body).toBe('{"key":"value"}')
  })
})

describe('ApiError', () => {
  it('includes status and detail properties', () => {
    const response = new Response(null, { status: 403, statusText: 'Forbidden' })
    const error = new ApiError(response, 'Access denied')
    expect(error.status).toBe(403)
    expect(error.detail).toBe('Access denied')
    expect(error.message).toBe('API Error 403: Access denied')
  })

  it('falls back to statusText when no detail provided', () => {
    const response = new Response(null, { status: 500, statusText: 'Internal Server Error' })
    const error = new ApiError(response)
    expect(error.detail).toBe('Internal Server Error')
    expect(error.message).toBe('API Error 500: Internal Server Error')
  })

  it('is an instance of Error', () => {
    const response = new Response(null, { status: 400, statusText: 'Bad Request' })
    const error = new ApiError(response)
    expect(error).toBeInstanceOf(Error)
  })
})

describe('api convenience methods', () => {
  let fetchSpy: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchSpy = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchSpy)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
  })

  it('api.get sends GET request', async () => {
    await api.get('/vaults')
    const [url] = fetchSpy.mock.calls[0]
    expect(url).toBe('/api/v1/vaults')
  })

  it('api.post sends POST with JSON body', async () => {
    await api.post('/vaults', { name: 'test' })
    const [, options] = fetchSpy.mock.calls[0]
    expect(options.method).toBe('POST')
    expect(options.body).toBe('{"name":"test"}')
  })

  it('api.post sends POST with undefined body when no data', async () => {
    await api.post('/trigger')
    const [, options] = fetchSpy.mock.calls[0]
    expect(options.method).toBe('POST')
    expect(options.body).toBeUndefined()
  })

  it('api.put sends PUT with JSON body', async () => {
    await api.put('/vaults/123', { name: 'updated' })
    const [, options] = fetchSpy.mock.calls[0]
    expect(options.method).toBe('PUT')
    expect(options.body).toBe('{"name":"updated"}')
  })

  it('api.patch sends PATCH with JSON body', async () => {
    await api.patch('/vaults/123', { name: 'patched' })
    const [, options] = fetchSpy.mock.calls[0]
    expect(options.method).toBe('PATCH')
    expect(options.body).toBe('{"name":"patched"}')
  })

  it('api.delete sends DELETE request', async () => {
    await api.delete('/vaults/123')
    const [url, options] = fetchSpy.mock.calls[0]
    expect(url).toBe('/api/v1/vaults/123')
    expect(options.method).toBe('DELETE')
  })

  it('api.getRaw returns raw Response', async () => {
    const result = await api.getRaw('/metrics')
    expect(result).toBeInstanceOf(Response)
  })
})
