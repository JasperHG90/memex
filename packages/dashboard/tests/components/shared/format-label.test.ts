import { describe, it, expect } from 'vitest'
import { formatLabel } from '@/components/shared/format-label'

describe('formatLabel', () => {
  it('replaces underscores with spaces and title-cases', () => {
    expect(formatLabel('memory_unit')).toBe('Memory Unit')
  })

  it('title-cases single word', () => {
    expect(formatLabel('note')).toBe('Note')
  })

  it('handles already capitalized input', () => {
    expect(formatLabel('Person')).toBe('Person')
  })

  it('handles multiple underscores', () => {
    expect(formatLabel('long_multi_word_label')).toBe('Long Multi Word Label')
  })

  it('handles empty string', () => {
    expect(formatLabel('')).toBe('')
  })
})
