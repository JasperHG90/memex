import { describe, it, expect, vi, afterEach } from 'vitest'
import { z } from 'zod'
import { validateResponse, validated, validateArrayResponse } from './validate'

const TestSchema = z.object({
  id: z.string(),
  name: z.string(),
})

describe('validateResponse', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('returns parsed data when valid', () => {
    const data = { id: '1', name: 'Test' }
    const result = validateResponse(TestSchema, data)
    expect(result).toEqual({ id: '1', name: 'Test' })
  })

  it('warns and returns raw data when invalid in dev mode', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const data = { id: 123, name: 'Test' } // id should be string
    const result = validateResponse(TestSchema, data)
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('[API Validation]'),
      expect.any(Array),
    )
    expect(result).toEqual(data) // raw data returned
  })
})

describe('validated', () => {
  it('creates a validated fetcher', async () => {
    const fetcher = vi.fn().mockResolvedValue({ id: '1', name: 'Test' })
    const validatedFetcher = validated(TestSchema, fetcher)
    const result = await validatedFetcher()
    expect(result).toEqual({ id: '1', name: 'Test' })
    expect(fetcher).toHaveBeenCalledOnce()
  })
})

describe('validateArrayResponse', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('returns validated array items', () => {
    const data = [
      { id: '1', name: 'A' },
      { id: '2', name: 'B' },
    ]
    const result = validateArrayResponse(TestSchema, data)
    expect(result).toEqual(data)
  })

  it('warns for invalid items but still returns them', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const data = [
      { id: '1', name: 'A' },
      { id: 123, name: 'B' }, // invalid id
    ]
    const result = validateArrayResponse(TestSchema, data)
    expect(warnSpy).toHaveBeenCalled()
    expect(result).toHaveLength(2)
  })

  it('warns when data is not an array', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const result = validateArrayResponse(TestSchema, 'not an array')
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('Expected array'),
      expect.anything(),
    )
    expect(result).toBe('not an array')
  })
})
