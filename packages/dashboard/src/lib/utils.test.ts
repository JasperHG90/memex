import { describe, it, expect } from 'vitest'
import { encodeBase64 } from './utils'

describe('encodeBase64', () => {
  it('encodes ASCII string correctly', () => {
    const result = encodeBase64('Hello, world!')
    expect(result).toBe(btoa('Hello, world!'))
  })

  it('encodes non-ASCII characters without throwing', () => {
    expect(() => encodeBase64('caf\u00e9')).not.toThrow()
  })

  it('round-trips unicode characters through encode/decode', () => {
    const input = '\u00e9\u00e8\u00ea'
    const encoded = encodeBase64(input)
    const decoded = new TextDecoder().decode(
      Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0))
    )
    expect(decoded).toBe(input)
  })

  it('round-trips emoji characters', () => {
    const input = 'Notes with emoji: \ud83d\udcdd\ud83e\udde0'
    const encoded = encodeBase64(input)
    const decoded = new TextDecoder().decode(
      Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0))
    )
    expect(decoded).toBe(input)
  })

  it('handles empty string', () => {
    expect(encodeBase64('')).toBe('')
  })

  it('round-trips CJK characters', () => {
    const input = '\u4f60\u597d\u4e16\u754c'
    const encoded = encodeBase64(input)
    const decoded = new TextDecoder().decode(
      Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0))
    )
    expect(decoded).toBe(input)
  })
})
